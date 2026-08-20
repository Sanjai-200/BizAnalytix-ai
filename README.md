# BizAnalytix AI

An AI assistant for business, organizational, and data-analysis questions.
Users log in, upload a CSV/XLS/XLSX/JSON dataset, and ask natural-language
questions about their own data. Built with Flask, Firebase Authentication,
Cloud Firestore, Pandas, and the Gemini API — designed to run entirely on
free tiers (Firebase Spark, Gemini free usage, Render free web service).

---

## 1. How it works

```
Browser → Firebase Authentication → Flask (app.py) → Pandas file processing
        → user-scoped Firestore → Flask retrieves authorized data
        → Gemini API → response → Browser
```

- The browser only ever talks to **Firebase Auth** (to log in) and to
  **Flask** (for everything else). It never talks to Firestore or Gemini
  directly.
- Every request to Flask carries a Firebase ID token. Flask verifies that
  token with the **Firebase Admin SDK** and gets a trusted `uid` back —
  the frontend can never claim a different uid, email, or role.
- Flask always fetches data scoped to that `uid` (e.g.
  `users/{uid}/datasets/...`) — never a global collection filtered
  afterward, except for the specific admin/developer cross-user features
  described in Section 4 below, which have their own explicit checks.
- **Gemini never touches Firestore.** Flask fetches the authorized data,
  summarizes it with Pandas, and only then sends that bounded summary to
  Gemini along with the question.
- **No manual dataset selection needed.** By default, every question is
  answered using ALL of the user's own uploaded datasets combined (each
  labeled by filename so Gemini can tell them apart). Clicking one
  dataset in the sidebar narrows a question down to just that file; the
  "×" on the resulting pill switches back to "all datasets."

Everything on the backend lives in a single file, **`app.py`**, as plain
functions. Frontend logic is a single **`static/script.js`** file shared
by the chat page, admin dashboard, and developer dashboard.

---

## 2. Project structure

```
bizanalytix-ai/
├── app.py                     # entire backend: routes + all logic
├── requirements.txt
├── .env.example                # copy to .env and fill in
├── .gitignore
├── README.md
├── firebase/
│   └── firestore.rules         # client-side security rules (defense in depth)
├── templates/
│   ├── login.html
│   ├── register.html
│   ├── index.html               # main chat app
│   ├── admin.html                # admin dashboard
│   └── developer.html            # developer dashboard
└── static/
    ├── style.css
    ├── script.js                # all frontend logic (chat + admin + developer)
    └── firebase-config.js       # your PUBLIC Firebase web app config
```

---

## 3. Role permission matrix

| Feature | Admin | Developer | User |
|---|---|---|---|
| Login | ✅ | ✅ | ✅ |
| Chat with AI | ✅ | ✅ | ✅ |
| Upload files | ✅ | ✅ | ✅ |
| View own datasets | ✅ | ✅ | ✅ |
| Delete own datasets | ✅ | ✅ | ✅ |
| Manage users | ✅ | ❌ | ❌ |
| Change user roles | ✅ | ❌ | ❌ |
| View system analytics | Full | Limited | ❌ |
| Manage AI configuration | ✅ | ✅ | ❌ |
| Manage chatbot configuration | ✅ | ✅ | ❌ |
| Access other users' data | Any (admin policy) | Only shared/authorized data | ❌ |
| Developer dashboard | Optional (can view) | ✅ | ❌ |
| Admin dashboard | ✅ | ❌ | ❌ |

Every one of these is enforced **server-side** in `app.py` via the
`require_auth` and `require_role(...)` decorators on each route — the
frontend only uses the role to decide what buttons/links to *show*, it
never decides what's actually *allowed*. You can change any of this by
editing the `require_role(...)` arguments on the relevant route.

### How each row is implemented

- **Manage users / change roles** — `/api/admin/users*` routes,
  `require_role("admin")` only.
- **View system analytics** — `/api/admin/overview`. Admins get
  `total_users`, `total_datasets`, `total_records`, and a role
  breakdown. Developers get only `total_datasets` and `total_records`
  (no per-user info). Users get a 403.
- **Manage AI/chatbot configuration** — `/api/config` (GET/POST),
  `require_role("admin", "developer")`. Stored in Firestore at
  `config/app_config` (model, temperature, system prompt, welcome
  message) and read fresh on every chat request in `ask_gemini()`.
- **Access other users' data** — implemented via a **dataset sharing**
  system. Any dataset owner can share a dataset with a specific
  developer/admin account by email (share icon next to a dataset in the
  chat sidebar). That grants the recipient read-only access to that one
  dataset — for chatting and viewing, never deleting. Admins can see and
  use *every* dataset from every account, per "admin policy," without
  needing to be explicitly shared with.
- **Developer / Admin dashboards** — separate pages (`/developer`,
  `/admin`) that call `/api/auth/verify` to get the caller's real role
  and show an "access denied" panel if it doesn't match.

---

## 4. Firestore data model

```
users/{uid}
  email, role ("admin" | "developer" | "user"), status ("active" | "disabled"), created_at

users/{uid}/datasets/{datasetId}
  filename, file_type, columns, record_count, status, uploaded_at,
  owner_uid, shared_with (array of uids granted read access)

users/{uid}/datasets/{datasetId}/records/{recordId}
  one row of the uploaded file (recordId is deterministic: "row_0", "row_1", ...
  so re-processing never creates duplicates)

users/{uid}/chats/{chatId}
  title, dataset_id, created_at, owner_uid

users/{uid}/chats/{chatId}/messages/{messageId}
  role ("user" | "assistant"), content, dataset_id, timestamp

config/app_config
  model, temperature, system_prompt, welcome_message, updated_by, updated_at
```

**Important:** the Flask backend uses the **Firebase Admin SDK**, which
bypasses Firestore security rules entirely. The rules in
`firebase/firestore.rules` only protect against a hypothetical direct
client-side access path — the real protection is that every Flask query
is always scoped with the verified `uid` from the ID token (plus the
explicit `shared_with` / admin checks for cross-user access). Never
trust a uid/role/dataset id sent by the browser.

---

## 5. Local setup

### 5.1 Firebase project

1. Create a project at [Firebase Console](https://console.firebase.google.com).
2. **Authentication** → Sign-in method → enable **Email/Password**.
3. **Firestore Database** → create a database (production mode is fine).
4. **Project Settings → General → Your apps** → add a **Web app**. Copy
   the config object into `static/firebase-config.js` (this is public
   config, not a secret). The app will show a yellow banner on every
   page until this is done correctly.
5. **Project Settings → Service accounts** → Generate new private key.
   Save the downloaded JSON as `serviceAccountKey.json` in the project
   root (already covered by `.gitignore` — never commit this file).

### 5.2 Gemini API key

Get a free key from [Google AI Studio](https://aistudio.google.com/apikey).

### 5.3 Environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in:
- `FLASK_SECRET_KEY` — any random string
- `FIREBASE_SERVICE_ACCOUNT_KEY_PATH` — path to the JSON key from step 5.1
- `GEMINI_API_KEY` — from step 5.2
- `ADMIN_EMAILS` — your own email, so your first account becomes admin

### 5.4 Install and run

```bash
pip install -r requirements.txt
python app.py
```

Open `http://localhost:5000`, register with the email listed in
`ADMIN_EMAILS`, and you'll land as an admin. Any other email registers as
a regular user. To test the developer role, register a second account
and promote it to "developer" from the Admin dashboard's user table.

No virtual environment or extra CLI tooling is required — just the two
commands above.

---

## 6. Deployment (GitHub + Render)

1. Push the project to a GitHub repository (`serviceAccountKey.json` and
   `.env` are already excluded by `.gitignore` — do **not** commit them).
2. On [Render](https://render.com), create a new **Web Service** from
   that repo.
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `gunicorn app:app`
3. Under **Environment**, add the same variables as your `.env` file
   (`FLASK_SECRET_KEY`, `GEMINI_API_KEY`, `GEMINI_MODEL`, `ADMIN_EMAILS`,
   the upload limits) **plus** `FIREBASE_SERVICE_ACCOUNT_KEY_PATH`.
4. For the service account key on Render (no persistent file system to
   upload to ahead of time): add an environment variable
   `FIREBASE_SERVICE_ACCOUNT_JSON` containing the full JSON key as a
   single-line string, then add this near the top of `app.py`, replacing
   the direct `credentials.Certificate(FIREBASE_KEY_PATH)` call:
   ```python
   import json, tempfile
   raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
   if raw:
       tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
       tmp.write(raw)
       tmp.close()
       FIREBASE_KEY_PATH = tmp.name
   ```
5. Deploy. Render gives you an HTTPS URL that works from any device/browser.
6. In Firebase Console → Authentication → Settings → **Authorized
   domains**, add your Render URL's domain so login works there too.

Render's free web service can sleep after inactivity — the first request
after idling may take a few seconds to wake up. This is expected on the
free tier.

---

## 7. Free-tier limits built into the app

- `MAX_FILE_SIZE_MB` (default 5 MB) — rejects larger uploads.
- `MAX_UPLOAD_ROWS` (default 5000) — extra rows in a file are trimmed
  before writing to Firestore, to stay within Firestore's free document
  quota.
- `MAX_CONTEXT_ROWS` (default 20) — only a small sample of rows, plus
  aggregated stats, is ever sent to Gemini — never the whole dataset.
- `MAX_CHAT_HISTORY` (default 10) — only the most recent messages are
  sent to Gemini as conversation context.
- No Firebase Cloud Storage is used — original files are parsed in
  memory and only the structured rows are stored in Firestore.

---

## 8. Phase-2 demo: proving user data isolation

Use two separate browser profiles (or one normal + one incognito window)
so both sessions stay logged in at once.

1. **Register two accounts:** User A and User B (neither email in
   `ADMIN_EMAILS`, so both are plain "user" role).
2. **User A** logs in, uploads `students_A.xlsx`, and asks a question
   like "how many students are in this dataset?" → answer reflects A's
   file only.
3. **User B** logs in (separate window), uploads `students_B.xlsx`, and
   asks the same style of question → answer reflects B's file only.
4. Confirm in each sidebar: **User A never sees `students_B.xlsx`** in
   their dataset list, and vice versa.
5. Confirm chat history isolation: A's chat list never shows B's chats.
6. **Edit data:** upload a corrected version of `students_A.xlsx` and ask
   A's next question again — the answer changes accordingly, proving
   responses are never using stale data.
7. **Unauthorized admin access:** while logged in as User A or B, call
   `GET /api/admin/users` → returns `403 Forbidden` (their server-side
   role is "user", not "admin").
8. **Sharing:** promote User B to "developer" from the Admin dashboard.
   Have User A share `students_A.xlsx` with User B's email. Log in as
   User B and confirm the dataset now appears under "Datasets shared
   with me" in the Developer dashboard — but User B still cannot see
   any of User A's *other*, unshared datasets.

---

## 9. Security checklist

- [x] Firebase ID token verified server-side on every protected route (`require_auth`)
- [x] Role checked server-side on every sensitive route, never trusted from the client (`require_role`)
- [x] Every Firestore read/write scoped to `users/{verified-uid}/...`, with explicit checks for the few cross-user (sharing/admin) routes
- [x] File type and size validated before parsing
- [x] Malformed files rejected with a safe error message (no stack traces returned)
- [x] Gemini API key and Firebase service account key only ever read from
      environment variables on the server — never sent to the browser
- [x] `static/firebase-config.js` contains only the public web app config
      (this is safe by design in Firebase — the private key is separate)
- [x] Firestore rules provided as a second layer of defense for any
      direct client access path
