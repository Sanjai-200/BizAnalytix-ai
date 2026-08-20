"""
BizAnalytix AI - Business & Organizational Data Analysis Assistant
--------------------------------------------------------------------
Everything the backend needs (auth, RBAC, file processing, Firestore
access, dataset sharing, AI config, and Gemini calls) lives in this
single file as plain functions, organized top-to-bottom:

  Config -> Firebase/Gemini init -> Auth helpers -> RBAC helpers
  -> File processing -> Dataset helpers -> Sharing/permission helpers
  -> Analysis helpers -> Chat helpers -> AI config helpers
  -> Gemini helper -> Page routes -> API routes

Flow:
  Browser -> Firebase Auth -> Flask (this file) -> Pandas -> Firestore
  -> Flask retrieves authorized data -> Gemini -> Flask -> Browser

Gemini never touches Firestore directly. Flask always authenticates,
authorizes, and fetches data BEFORE anything is sent to Gemini.

Role permission matrix (enforced entirely server-side, never trusted
from the client):

  Feature                        Admin   Developer   User
  --------------------------------------------------------
  Login                           yes      yes        yes
  Chat with AI                    yes      yes        yes
  Upload files                    yes      yes        yes
  View own datasets               yes      yes        yes
  Delete own datasets             yes      yes        yes
  Manage users                    yes      no         no
  Change user roles               yes      no         no
  View system analytics           full    limited      no
  Manage AI configuration         yes      yes         no
  Manage chatbot configuration    yes      yes         no
  Access other users' data     any (policy) shared-only  no
  Developer dashboard          optional     yes         no
  Admin dashboard                 yes       no          no
"""

import os
import io
import json
import uuid
import math
from datetime import datetime, timezone
from functools import wraps

import pandas as pd
from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv

import firebase_admin
from firebase_admin import credentials, auth as fb_auth, firestore

from google import genai
from google.genai import types as genai_types

load_dotenv()

# ---------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------

FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
FIREBASE_KEY_PATH = os.environ.get("FIREBASE_SERVICE_ACCOUNT_KEY_PATH", "./serviceAccountKey.json")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "") 
DEFAULT_GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}

MAX_FILE_SIZE_MB = int(os.environ.get("MAX_FILE_SIZE_MB", "5"))
MAX_UPLOAD_ROWS = int(os.environ.get("MAX_UPLOAD_ROWS", "5000"))
MAX_CONTEXT_ROWS = int(os.environ.get("MAX_CONTEXT_ROWS", "20"))
MAX_CHAT_HISTORY = int(os.environ.get("MAX_CHAT_HISTORY", "10"))
MAX_DATASETS_IN_CONTEXT = int(os.environ.get("MAX_DATASETS_IN_CONTEXT", "5"))

ALLOWED_EXTENSIONS = {"csv", "xls", "xlsx", "json"}
ROLES = {"admin", "developer", "user"}

DEFAULT_SYSTEM_PROMPT = """You are BizAnalytix AI, an assistant that ONLY helps with business,
organizational, management, and data-analysis questions about the user's own uploaded
dataset(s) and general business/analytics concepts.

Rules:
- Stay strictly within business, organization, management, and data-analysis topics.
- If asked something unrelated (coding help, personal advice, general trivia, etc.),
  politely decline and redirect to business/data topics.
- When dataset context is provided below, base numeric answers on that context. Do not
  invent numbers that are not supported by the provided data or summary statistics.
- If the dataset context does not contain enough information to answer, say so clearly
  instead of guessing.
- Be concise and clear. Use short paragraphs or bullet points for summaries.
- If context from more than one dataset is provided, each will be clearly
  labeled with its filename. Only combine numbers across datasets when it
  makes sense to (e.g. same kind of data); otherwise answer per-dataset and
  say which file each number came from.
"""

DEFAULT_WELCOME_MESSAGE = "Ask a business or data question below."

# ---------------------------------------------------------------------
# FLASK APP
# ---------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_SIZE_MB * 1024 * 1024

# ---------------------------------------------------------------------
# FIREBASE ADMIN INIT
# ---------------------------------------------------------------------

if not firebase_admin._apps:
    cred = credentials.Certificate(FIREBASE_KEY_PATH)
    firebase_admin.initialize_app(cred)

db = firestore.client()

# ---------------------------------------------------------------------
# GEMINI CLIENT INIT
# ---------------------------------------------------------------------

gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


# ---------------------------------------------------------------------
# AUTH HELPERS
# ---------------------------------------------------------------------

def verify_token_from_header():
    """
    Reads 'Authorization: Bearer <idToken>' from the request, verifies it
    with the Firebase Admin SDK, and returns the decoded token (which
    includes the trusted uid and email). Returns None if missing/invalid.

    This is the ONLY source of truth for who the user is. The frontend
    can never claim a uid, email, or role directly - it is always re-
    derived here from the verified token.
    """
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    id_token = header.split("Bearer ", 1)[1].strip()
    try:
        return fb_auth.verify_id_token(id_token)
    except Exception:
        return None


def get_or_create_user_profile(decoded_token):
    """
    Looks up (or creates) the Firestore profile for this uid. This is
    where the user's role is stored server-side. The very first login of
    an email listed in ADMIN_EMAILS becomes an admin automatically;
    everyone else starts as 'user'. Only an existing admin can promote
    other accounts afterward (see /api/admin/users/<uid>/role).
    """
    uid = decoded_token["uid"]
    email = decoded_token.get("email", "")
    profile_ref = db.collection("users").document(uid)
    profile = profile_ref.get()

    if profile.exists:
        return profile.to_dict()

    role = "admin" if email.lower() in ADMIN_EMAILS else "user"
    data = {
        "uid": uid,
        "email": email,
        "role": role,
        "status": "active",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    profile_ref.set(data)
    return data


def require_auth(f):
    """Decorator: verifies the Firebase ID token and loads the user's
    server-side profile (with their real role) before running the route.
    Injects `uid` and `profile` as keyword arguments."""

    @wraps(f)
    def wrapper(*args, **kwargs):
        decoded = verify_token_from_header()
        if not decoded:
            return jsonify({"error": "Unauthorized. Missing or invalid token."}), 401
        profile = get_or_create_user_profile(decoded)
        if profile.get("status") == "disabled":
            return jsonify({"error": "This account has been disabled."}), 403
        kwargs["uid"] = decoded["uid"]
        kwargs["profile"] = profile
        return f(*args, **kwargs)

    return wrapper


def require_role(*allowed_roles):
    """Decorator factory: use AFTER @require_auth. Rejects the request if
    the user's server-side role is not in allowed_roles."""

    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            profile = kwargs.get("profile", {})
            if profile.get("role") not in allowed_roles:
                return jsonify({"error": "Forbidden. Your role does not have access to this feature."}), 403
            return f(*args, **kwargs)

        return wrapper

    return decorator


# ---------------------------------------------------------------------
# FILE PROCESSING HELPERS
# ---------------------------------------------------------------------

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def clean_value(v):
    """Normalize a single value so it is safe to store in Firestore and
    safe to JSON-serialize (Firestore rejects NaN/Infinity)."""
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, (pd.Timestamp, datetime)):
        return v.isoformat()
    return v


def dataframe_to_records(df):
    """Convert a pandas DataFrame into a list of plain dicts with clean
    values, ready to store in Firestore."""
    records = []
    for row in df.to_dict(orient="records"):
        clean_row = {str(k): clean_value(v) for k, v in row.items()}
        records.append(clean_row)
    return records


def parse_uploaded_file(file_storage, filename):
    """
    Parses an uploaded CSV/XLS/XLSX/JSON file into a pandas DataFrame.
    Raises ValueError with a safe, user-facing message on bad input.
    """
    ext = filename.rsplit(".", 1)[1].lower()
    raw = file_storage.read()

    if len(raw) == 0:
        raise ValueError("The uploaded file is empty.")

    try:
        if ext == "csv":
            df = pd.read_csv(io.BytesIO(raw))
        elif ext in ("xls", "xlsx"):
            df = pd.read_excel(io.BytesIO(raw))
        elif ext == "json":
            parsed = json.loads(raw.decode("utf-8"))
            if isinstance(parsed, dict):
                # Allow either a single object or {"records": [...]}.
                parsed = parsed.get("records", [parsed]) if "records" in parsed else [parsed]
            if not isinstance(parsed, list):
                raise ValueError("JSON file must contain an object or an array of objects.")
            df = pd.DataFrame(parsed)
        else:
            raise ValueError("Unsupported file type.")
    except ValueError:
        raise
    except Exception:
        raise ValueError("Could not parse the file. Please check that it is a valid CSV, Excel, or JSON file.")

    if df.empty:
        raise ValueError("The file has no rows of data.")

    if len(df) > MAX_UPLOAD_ROWS:
        df = df.head(MAX_UPLOAD_ROWS)  # bounded, so we never blow past free-tier limits

    df.columns = [str(c).strip() for c in df.columns]
    return df


# ---------------------------------------------------------------------
# DATASET (FIRESTORE) HELPERS
# ---------------------------------------------------------------------

def save_dataset(uid, filename, df):
    """Stores dataset metadata + records under users/{uid}/datasets/{id}.
    Uses batched writes (Firestore batches max at 500 writes)."""
    dataset_id = str(uuid.uuid4())
    dataset_ref = db.collection("users").document(uid).collection("datasets").document(dataset_id)

    records = dataframe_to_records(df)

    dataset_ref.set({
        "dataset_id": dataset_id,
        "owner_uid": uid,
        "filename": filename,
        "file_type": filename.rsplit(".", 1)[1].lower(),
        "columns": list(df.columns),
        "record_count": len(records),
        "status": "ready",
        "shared_with": [],
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    })

    records_ref = dataset_ref.collection("records")
    batch = db.batch()
    batch_count = 0
    for i, record in enumerate(records):
        record_id = f"row_{i}"  # deterministic id avoids accidental duplicates on re-upload
        batch.set(records_ref.document(record_id), record)
        batch_count += 1
        if batch_count == 400:
            batch.commit()
            batch = db.batch()
            batch_count = 0
    if batch_count > 0:
        batch.commit()

    return dataset_id, len(records)


def list_datasets(uid):
    """Only ever queries the caller's own scoped collection - never a
    global collection filtered afterward."""
    docs = db.collection("users").document(uid).collection("datasets") \
        .order_by("uploaded_at", direction=firestore.Query.DESCENDING).stream()
    return [d.to_dict() for d in docs]


def get_latest_dataset_id(uid):
    """Returns the most recently uploaded dataset id for this user, or
    None if they haven't uploaded anything yet."""
    docs = db.collection("users").document(uid).collection("datasets") \
        .order_by("uploaded_at", direction=firestore.Query.DESCENDING).limit(1).stream()
    for d in docs:
        return d.id
    return None


def build_multi_dataset_context(uid, limit_datasets=MAX_DATASETS_IN_CONTEXT):
    """
    Builds a combined context across ALL of a user's own uploaded
    datasets, so questions can be answered without picking a single file
    first. Each dataset gets its own labeled section (full stats +
    a bounded sample), so Gemini can tell them apart and cite the right
    one. Bounded by MAX_DATASETS_IN_CONTEXT and MAX_CONTEXT_ROWS per
    dataset so the request never grows unbounded as someone uploads
    more files over time.
    """
    all_meta = list_datasets(uid)  # already sorted most-recent-first
    if not all_meta:
        return None, 0, 0

    selected_meta = all_meta[:limit_datasets]
    parts = []
    for meta in selected_meta:
        df = get_dataset_dataframe(uid, meta["dataset_id"], limit=MAX_CONTEXT_ROWS)
        if df.empty:
            continue
        summary = build_dataset_summary(df)
        parts.append(
            f"=== Dataset: {meta.get('filename')} (dataset_id: {meta.get('dataset_id')}) ===\n"
            f"Total rows: {summary['row_count']}\n"
            f"Columns: {', '.join(summary['columns'])}\n"
            f"Numeric column statistics (count, mean, min, max, sum):\n"
            f"{json.dumps(summary['numeric_stats'], indent=2)}\n"
            f"Top values for text/categorical columns:\n"
            f"{json.dumps(summary['top_categorical_values'], indent=2)}\n"
            f"Sample rows (first {len(summary['sample_rows'])} of {summary['row_count']}):\n"
            f"{json.dumps(summary['sample_rows'], indent=2)}\n"
        )

    if not parts:
        return None, 0, 0

    context_text = (
        f"The user has {len(all_meta)} dataset(s) uploaded. Below are the "
        f"{len(parts)} most recently uploaded ones. When answering, state which "
        f"dataset(s) a number came from if more than one is relevant.\n\n"
        + "\n".join(parts)
    )
    return context_text, len(parts), len(all_meta)


def get_dataset_meta(owner_uid, dataset_id):
    ref = db.collection("users").document(owner_uid).collection("datasets").document(dataset_id)
    doc = ref.get()
    return doc.to_dict() if doc.exists else None


def get_dataset_dataframe(owner_uid, dataset_id, limit=None):
    """Fetches dataset records from the given owner's scoped path and
    returns them as a DataFrame. Callers are responsible for verifying
    the requester is allowed to read this owner's data first."""
    records_ref = db.collection("users").document(owner_uid).collection("datasets") \
        .document(dataset_id).collection("records")
    query = records_ref.limit(limit) if limit else records_ref
    docs = query.stream()
    rows = [d.to_dict() for d in docs]
    return pd.DataFrame(rows)


def delete_dataset(uid, dataset_id):
    dataset_ref = db.collection("users").document(uid).collection("datasets").document(dataset_id)
    records_ref = dataset_ref.collection("records")

    while True:
        docs = list(records_ref.limit(400).stream())
        if not docs:
            break
        batch = db.batch()
        for d in docs:
            batch.delete(d.reference)
        batch.commit()

    dataset_ref.delete()


# ---------------------------------------------------------------------
# CROSS-USER SHARING / PERMISSION HELPERS
# ("Access other users' data" row of the permission matrix)
# ---------------------------------------------------------------------

def can_access_foreign_dataset(role, requester_uid, dataset_meta):
    """
    Admin: full access to any dataset (per admin policy).
    Developer: only datasets explicitly shared with them.
    User: never.
    """
    if role == "admin":
        return True
    if role == "developer":
        return requester_uid in (dataset_meta.get("shared_with") or [])
    return False


def find_user_by_email(email):
    docs = db.collection("users").where("email", "==", email).limit(1).stream()
    for d in docs:
        return d.to_dict()
    return None


def share_dataset(owner_uid, dataset_id, target_email):
    meta = get_dataset_meta(owner_uid, dataset_id)
    if not meta:
        return None, "Dataset not found."

    target = find_user_by_email(target_email)
    if not target:
        return None, "No account found with that email."
    if target["uid"] == owner_uid:
        return None, "You already own this dataset."
    if target.get("role") not in ("developer", "admin"):
        return None, "You can only share datasets with developer or admin accounts."

    ref = db.collection("users").document(owner_uid).collection("datasets").document(dataset_id)
    ref.update({"shared_with": firestore.ArrayUnion([target["uid"]])})
    return target["email"], None


def unshare_dataset(owner_uid, dataset_id, target_uid):
    ref = db.collection("users").document(owner_uid).collection("datasets").document(dataset_id)
    ref.update({"shared_with": firestore.ArrayRemove([target_uid])})


def list_accessible_foreign_datasets(uid, role):
    """
    Admin: every dataset from every user (admin policy = full visibility).
    Developer: only datasets explicitly shared with them.
    """
    if role == "admin":
        docs = db.collection_group("datasets").stream()
    elif role == "developer":
        docs = db.collection_group("datasets").where("shared_with", "array_contains", uid).stream()
    else:
        return []

    results = []
    owner_email_cache = {}
    for d in docs:
        data = d.to_dict()
        if data.get("owner_uid") == uid:
            continue  # skip the viewer's own datasets, those already show in "My datasets"
        owner_uid = data.get("owner_uid")
        if owner_uid not in owner_email_cache:
            owner_doc = db.collection("users").document(owner_uid).get()
            owner_email_cache[owner_uid] = owner_doc.to_dict().get("email") if owner_doc.exists else "unknown"
        data["owner_email"] = owner_email_cache[owner_uid]
        results.append(data)
    return results


# ---------------------------------------------------------------------
# DATA ANALYSIS HELPERS
# ---------------------------------------------------------------------

def build_dataset_summary(df):
    """
    Builds a compact, deterministic summary of the dataset using Pandas
    (row/column counts, per-column stats, small sample) instead of ever
    sending the full dataset to Gemini. This keeps requests small and
    keeps numeric answers grounded in real calculations.
    """
    summary = {
        "row_count": len(df),
        "columns": list(df.columns),
    }

    numeric_cols = df.select_dtypes(include="number").columns
    text_cols = [c for c in df.columns if c not in numeric_cols]

    numeric_stats = {}
    for col in numeric_cols:
        series = df[col].dropna()
        if series.empty:
            continue
        numeric_stats[col] = {
            "count": int(series.count()),
            "mean": round(float(series.mean()), 2),
            "min": round(float(series.min()), 2),
            "max": round(float(series.max()), 2),
            "sum": round(float(series.sum()), 2),
        }
    summary["numeric_stats"] = numeric_stats

    categorical_stats = {}
    for col in text_cols:
        counts = df[col].astype(str).value_counts().head(5)
        if not counts.empty:
            categorical_stats[col] = counts.to_dict()
    summary["top_categorical_values"] = categorical_stats

    sample_df = df.head(MAX_CONTEXT_ROWS)
    summary["sample_rows"] = dataframe_to_records(sample_df)

    return summary


def build_gemini_context(requester_uid, role, dataset_id, owner_uid=None):
    """
    Loads a dataset and turns it into a bounded text summary safe to
    send to Gemini. If owner_uid differs from requester_uid, this is a
    cross-user (shared/admin) request and must pass permission checks
    before any data is read.
    """
    owner_uid = owner_uid or requester_uid
    meta = get_dataset_meta(owner_uid, dataset_id)
    if not meta:
        return None, "Dataset not found."

    if owner_uid != requester_uid and not can_access_foreign_dataset(role, requester_uid, meta):
        return None, "You are not authorized to use this dataset."

    df = get_dataset_dataframe(owner_uid, dataset_id)
    if df.empty:
        return None, "Dataset has no records."

    summary = build_dataset_summary(df)
    context_text = (
        f"Dataset filename: {meta.get('filename')}\n"
        f"Total rows: {summary['row_count']}\n"
        f"Columns: {', '.join(summary['columns'])}\n\n"
        f"Numeric column statistics (count, mean, min, max, sum):\n"
        f"{json.dumps(summary['numeric_stats'], indent=2)}\n\n"
        f"Top values for text/categorical columns:\n"
        f"{json.dumps(summary['top_categorical_values'], indent=2)}\n\n"
        f"Sample rows (first {len(summary['sample_rows'])} of {summary['row_count']}):\n"
        f"{json.dumps(summary['sample_rows'], indent=2)}\n"
    )
    return context_text, None


# ---------------------------------------------------------------------
# CHAT (FIRESTORE) HELPERS
# ---------------------------------------------------------------------

def create_chat_if_needed(uid, chat_id, dataset_id=None):
    chat_ref = db.collection("users").document(uid).collection("chats").document(chat_id)
    if not chat_ref.get().exists:
        chat_ref.set({
            "chat_id": chat_id,
            "owner_uid": uid,
            "dataset_id": dataset_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "title": "New chat",
        })
    return chat_ref


def save_chat_message(uid, chat_id, role, content, dataset_id=None):
    chat_ref = create_chat_if_needed(uid, chat_id, dataset_id)
    message_id = str(uuid.uuid4())
    chat_ref.collection("messages").document(message_id).set({
        "message_id": message_id,
        "role": role,
        "content": content,
        "dataset_id": dataset_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    if role == "user":
        chat_snap = chat_ref.get().to_dict()
        if chat_snap and chat_snap.get("title") == "New chat":
            chat_ref.update({"title": content[:60]})


def get_chat_history(uid, chat_id, limit=MAX_CHAT_HISTORY):
    msgs_ref = db.collection("users").document(uid).collection("chats") \
        .document(chat_id).collection("messages") \
        .order_by("timestamp", direction=firestore.Query.DESCENDING).limit(limit)
    docs = list(msgs_ref.stream())
    docs.reverse()
    return [d.to_dict() for d in docs]


def list_chats(uid):
    docs = db.collection("users").document(uid).collection("chats") \
        .order_by("created_at", direction=firestore.Query.DESCENDING).stream()
    return [d.to_dict() for d in docs]


# ---------------------------------------------------------------------
# AI / CHATBOT CONFIGURATION HELPERS
# ("Manage AI configuration" + "Manage chatbot configuration" rows)
# ---------------------------------------------------------------------

CONFIG_DOC = db.collection("config").document("app_config")


def get_app_config():
    doc = CONFIG_DOC.get()
    data = doc.to_dict() if doc.exists else {}
    return {
        "model": data.get("model") or DEFAULT_GEMINI_MODEL,
        "temperature": data.get("temperature") if data.get("temperature") is not None else 0.4,
        "system_prompt": data.get("system_prompt") or DEFAULT_SYSTEM_PROMPT,
        "welcome_message": data.get("welcome_message") or DEFAULT_WELCOME_MESSAGE,
    }


def update_app_config(updates, uid):
    clean = {}
    if "model" in updates and str(updates["model"]).strip():
        clean["model"] = str(updates["model"]).strip()
    if "temperature" in updates:
        try:
            temp = float(updates["temperature"])
            clean["temperature"] = max(0.0, min(1.0, temp))
        except (TypeError, ValueError):
            pass
    if "system_prompt" in updates and str(updates["system_prompt"]).strip():
        clean["system_prompt"] = str(updates["system_prompt"]).strip()[:4000]
    if "welcome_message" in updates and str(updates["welcome_message"]).strip():
        clean["welcome_message"] = str(updates["welcome_message"]).strip()[:200]

    clean["updated_by"] = uid
    clean["updated_at"] = datetime.now(timezone.utc).isoformat()
    CONFIG_DOC.set(clean, merge=True)
    return get_app_config()


# ---------------------------------------------------------------------
# GEMINI HELPER
# ---------------------------------------------------------------------

def ask_gemini(question, history, dataset_context=None):
    """
    Sends the question to Gemini along with recent chat history and (if
    available) the bounded, authorized dataset context. Firestore
    datasets are never touched from here - everything Gemini sees was
    already fetched and authorized by Flask before this function runs.
    Model, temperature, and the system prompt come from the live AI
    configuration (editable by admin/developer), falling back to
    built-in defaults.
    """
    if gemini_client is None:
        return "Gemini is not configured. Please set GEMINI_API_KEY in the environment."

    config = get_app_config()

    contents = []
    for msg in history:
        role = "model" if msg["role"] == "assistant" else "user"
        contents.append(genai_types.Content(role=role, parts=[genai_types.Part(text=msg["content"])]))

    final_question = question
    if dataset_context:
        final_question = (
            f"Use the following dataset context to answer the question if relevant.\n\n"
            f"--- DATASET CONTEXT ---\n{dataset_context}\n--- END CONTEXT ---\n\n"
            f"Question: {question}"
        )
    contents.append(genai_types.Content(role="user", parts=[genai_types.Part(text=final_question)]))

    try:
        response = gemini_client.models.generate_content(
            model=config["model"],
            contents=contents,
            config=genai_types.GenerateContentConfig(
                system_instruction=config["system_prompt"],
                temperature=config["temperature"],
            ),
        )
        return response.text
    except Exception as e:
        return f"Sorry, I couldn't get a response from Gemini right now. ({str(e)[:150]})"


# ---------------------------------------------------------------------
# PAGE ROUTES (frontend checks auth/role client-side for UI purposes
# only; real protection for data always happens on /api/* routes)
# ---------------------------------------------------------------------

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/login")
def login_page():
    return render_template("login.html")


@app.route("/register")
def register_page():
    return render_template("register.html")


@app.route("/admin")
def admin_page():
    return render_template("admin.html")


@app.route("/developer")
def developer_page():
    return render_template("developer.html")


# ---------------------------------------------------------------------
# AUTH API
# ---------------------------------------------------------------------

@app.route("/api/auth/verify", methods=["POST"])
@require_auth
def api_auth_verify(uid, profile):
    """Called right after Firebase client-side login. Confirms the token
    is valid and returns the user's real server-side role/profile."""
    config = get_app_config()
    return jsonify({
        "uid": uid,
        "email": profile.get("email"),
        "role": profile.get("role"),
        "welcome_message": config["welcome_message"],
    })


# ---------------------------------------------------------------------
# UPLOAD + DATASETS API (all roles)
# ---------------------------------------------------------------------

@app.route("/api/upload", methods=["POST"])
@require_auth
def api_upload(uid, profile):
    if "file" not in request.files:
        return jsonify({"error": "No file provided."}), 400

    file = request.files["file"]
    filename = file.filename or ""

    if filename == "" or not allowed_file(filename):
        return jsonify({"error": "Unsupported file type. Use CSV, XLS, XLSX, or JSON."}), 400

    try:
        df = parse_uploaded_file(file, filename)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    dataset_id, record_count = save_dataset(uid, filename, df)
    return jsonify({
        "message": "File uploaded and processed successfully.",
        "dataset_id": dataset_id,
        "filename": filename,
        "record_count": record_count,
        "columns": list(df.columns),
    })


@app.route("/api/datasets", methods=["GET"])
@require_auth
def api_list_datasets(uid, profile):
    return jsonify({"datasets": list_datasets(uid)})


@app.route("/api/datasets/<dataset_id>", methods=["GET"])
@require_auth
def api_get_dataset(uid, profile, dataset_id):
    meta = get_dataset_meta(uid, dataset_id)
    if not meta:
        return jsonify({"error": "Dataset not found."}), 404
    df = get_dataset_dataframe(uid, dataset_id, limit=MAX_CONTEXT_ROWS)
    return jsonify({"meta": meta, "preview_rows": dataframe_to_records(df)})


@app.route("/api/datasets/<dataset_id>", methods=["DELETE"])
@require_auth
def api_delete_dataset(uid, profile, dataset_id):
    meta = get_dataset_meta(uid, dataset_id)
    if not meta:
        return jsonify({"error": "Dataset not found."}), 404
    delete_dataset(uid, dataset_id)
    return jsonify({"message": "Dataset deleted."})


@app.route("/api/datasets/<dataset_id>/share", methods=["POST"])
@require_auth
def api_share_dataset(uid, profile, dataset_id):
    """Owner shares one of their own datasets with a developer/admin
    account by email. Per policy, plain "user" accounts cannot be share
    targets - only developer/admin accounts can be granted cross-user
    access."""
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip()
    if not email:
        return jsonify({"error": "Email is required."}), 400

    shared_email, err = share_dataset(uid, dataset_id, email)
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"message": f"Dataset shared with {shared_email}."})


@app.route("/api/datasets/<dataset_id>/share/<target_uid>", methods=["DELETE"])
@require_auth
def api_unshare_dataset(uid, profile, dataset_id, target_uid):
    meta = get_dataset_meta(uid, dataset_id)
    if not meta:
        return jsonify({"error": "Dataset not found."}), 404
    unshare_dataset(uid, dataset_id, target_uid)
    return jsonify({"message": "Access removed."})


# ---------------------------------------------------------------------
# SHARED / CROSS-USER DATASETS API (admin: all data, developer: shared only)
# ---------------------------------------------------------------------

@app.route("/api/shared-datasets", methods=["GET"])
@require_auth
@require_role("admin", "developer")
def api_list_shared_datasets(uid, profile):
    return jsonify({"datasets": list_accessible_foreign_datasets(uid, profile["role"])})


@app.route("/api/shared-datasets/<owner_uid>/<dataset_id>", methods=["GET"])
@require_auth
@require_role("admin", "developer")
def api_get_shared_dataset(uid, profile, owner_uid, dataset_id):
    meta = get_dataset_meta(owner_uid, dataset_id)
    if not meta:
        return jsonify({"error": "Dataset not found."}), 404
    if not can_access_foreign_dataset(profile["role"], uid, meta):
        return jsonify({"error": "You are not authorized to view this dataset."}), 403
    df = get_dataset_dataframe(owner_uid, dataset_id, limit=MAX_CONTEXT_ROWS)
    return jsonify({"meta": meta, "preview_rows": dataframe_to_records(df)})


# ---------------------------------------------------------------------
# CHAT API (all roles; dataset_owner_uid lets admin/developer chat
# against a dataset they don't own, if they're authorized for it)
# ---------------------------------------------------------------------

@app.route("/api/chat", methods=["POST"])
@require_auth
def api_chat(uid, profile):
    body = request.get_json(silent=True) or {}
    question = (body.get("message") or "").strip()
    chat_id = body.get("chat_id") or str(uuid.uuid4())
    dataset_id = body.get("dataset_id")
    dataset_owner_uid = body.get("dataset_owner_uid") or uid

    if not question:
        return jsonify({"error": "Message cannot be empty."}), 400

    dataset_context = None
    datasets_used = None
    datasets_total = None

    if dataset_id:
        # Person explicitly narrowed the question to one specific
        # dataset (their own, or a shared/admin-visible one).
        dataset_context, err = build_gemini_context(uid, profile["role"], dataset_id, owner_uid=dataset_owner_uid)
        if err:
            return jsonify({"error": err}), 403 if "not authorized" in err else 404
    else:
        # No dataset picked -> automatically answer from ALL of the
        # user's own uploaded datasets combined, instead of requiring a
        # manual selection every time.
        dataset_context, datasets_used, datasets_total = build_multi_dataset_context(uid)

    history = get_chat_history(uid, chat_id)

    save_chat_message(uid, chat_id, "user", question, dataset_id)
    answer = ask_gemini(question, history, dataset_context)
    save_chat_message(uid, chat_id, "assistant", answer, dataset_id)

    return jsonify({
        "chat_id": chat_id,
        "answer": answer,
        "datasets_used": datasets_used,
        "datasets_total": datasets_total,
    })


@app.route("/api/chats", methods=["GET"])
@require_auth
def api_list_chats(uid, profile):
    return jsonify({"chats": list_chats(uid)})


@app.route("/api/chats/<chat_id>", methods=["GET"])
@require_auth
def api_get_chat(uid, profile, chat_id):
    messages = get_chat_history(uid, chat_id, limit=200)
    return jsonify({"chat_id": chat_id, "messages": messages})


# ---------------------------------------------------------------------
# AI / CHATBOT CONFIGURATION API (admin + developer only)
# ---------------------------------------------------------------------

@app.route("/api/config", methods=["GET"])
@require_auth
@require_role("admin", "developer")
def api_get_config(uid, profile):
    return jsonify(get_app_config())


@app.route("/api/config", methods=["POST"])
@require_auth
@require_role("admin", "developer")
def api_update_config(uid, profile):
    body = request.get_json(silent=True) or {}
    updated = update_app_config(body, uid)
    return jsonify({"message": "Configuration updated.", "config": updated})


# ---------------------------------------------------------------------
# ANALYTICS API (admin: full, developer: limited, user: no access)
# ---------------------------------------------------------------------

@app.route("/api/admin/overview", methods=["GET"])
@require_auth
@require_role("admin", "developer")
def api_admin_overview(uid, profile):
    users = list(db.collection("users").stream())

    total_datasets = 0
    total_records = 0
    for u in users:
        datasets = list(u.reference.collection("datasets").stream())
        total_datasets += len(datasets)
        for ds in datasets:
            total_records += ds.to_dict().get("record_count", 0)

    if profile["role"] == "admin":
        role_counts = {"admin": 0, "developer": 0, "user": 0}
        for u in users:
            role_counts[u.to_dict().get("role", "user")] = role_counts.get(u.to_dict().get("role", "user"), 0) + 1
        return jsonify({
            "total_users": len(users),
            "total_datasets": total_datasets,
            "total_records": total_records,
            "role_counts": role_counts,
        })

    # Developer: limited analytics - no per-user breakdown or user count.
    return jsonify({
        "total_datasets": total_datasets,
        "total_records": total_records,
    })


# ---------------------------------------------------------------------
# USER MANAGEMENT API (admin only)
# ---------------------------------------------------------------------

@app.route("/api/admin/users", methods=["GET"])
@require_auth
@require_role("admin")
def api_admin_list_users(uid, profile):
    docs = db.collection("users").stream()
    return jsonify({"users": [d.to_dict() for d in docs]})


@app.route("/api/admin/users/<target_uid>/role", methods=["POST"])
@require_auth
@require_role("admin")
def api_admin_set_role(uid, profile, target_uid):
    body = request.get_json(silent=True) or {}
    new_role = body.get("role")
    if new_role not in ROLES:
        return jsonify({"error": f"Role must be one of {sorted(ROLES)}."}), 400

    ref = db.collection("users").document(target_uid)
    if not ref.get().exists:
        return jsonify({"error": "User not found."}), 404

    ref.update({"role": new_role})
    return jsonify({"message": f"Role updated to {new_role}."})


@app.route("/api/admin/users/<target_uid>/status", methods=["POST"])
@require_auth
@require_role("admin")
def api_admin_set_status(uid, profile, target_uid):
    body = request.get_json(silent=True) or {}
    new_status = body.get("status")
    if new_status not in ("active", "disabled"):
        return jsonify({"error": "Status must be 'active' or 'disabled'."}), 400

    ref = db.collection("users").document(target_uid)
    if not ref.get().exists:
        return jsonify({"error": "User not found."}), 404

    ref.update({"status": new_status})
    return jsonify({"message": f"Status updated to {new_status}."})


@app.route("/api/admin/users/<target_uid>/datasets", methods=["GET"])
@require_auth
@require_role("admin")
def api_admin_user_datasets(uid, profile, target_uid):
    """Admin-only: inspect any user's dataset list (metadata only),
    per the 'admin policy' row of the permission matrix."""
    return jsonify({"datasets": list_datasets(target_uid)})


# ---------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
