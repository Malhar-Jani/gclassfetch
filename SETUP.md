# Setup Guide: Google Cloud Credentials

To download your files directly from Google without third-party servers, you create your own desktop OAuth credentials in Google Cloud. This takes about 3 minutes.

---

## Prerequisites

- Python 3.8 or higher ([python.org](https://www.python.org/downloads/))
- For Windows users: ensure "Add Python to PATH" is checked during Python installation.

---

## Step 1: Install Python dependencies

Open a terminal in the project folder and run:

```bash
pip install -r requirements.txt
```

---

## Step 2: Google Cloud Console setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/) and sign in.
2. At the top bar, click **Select a project > New Project**. Name it anything (e.g. `gclassfetch`) and click **Create**.
3. **Enable APIs**:
   - In the left menu, open **APIs & Services > Library**.
   - Search for **Google Classroom API** and click **Enable**.
   - Search for **Google Drive API** and click **Enable**.
4. **OAuth Consent Screen**:
   - In the left menu, open **APIs & Services > OAuth consent screen**.
   - Select **External** and click **Create**.
   - Enter an app name (e.g. `gclassfetch`) and your email for support and developer contacts.
   - Click **Save and Continue** until you reach **Test users**.
   - Under **Test users**, click **+ ADD USERS**. Add the exact Google or school email address you use for Classroom.
   - Click **Save and Continue** to finish.
5. **Create Credentials**:
   - In the left menu, open **APIs & Services > Credentials**.
   - Click **+ CREATE CREDENTIALS** (at the top) and select **OAuth client ID**.
   - Set Application type to **Desktop app**.
   - Click **Create**.
   - In the popup, click **DOWNLOAD JSON**.
6. **Move and rename**:
   - Place the downloaded JSON file in this project folder.
   - Rename it to:
     ```text
     credentials.json
     ```

---

## Step 3: Run the app

- **Windows**: Double-click `run.bat` or run:
  ```cmd
  python app.py
  ```
- **macOS / Linux**:
  ```bash
  chmod +x run.sh
  ./run.sh
  ```

---

## Step 4: First-time sign in

1. Your browser will open `http://localhost:5328`.
2. Click **Sign in with Google**.
3. Pick the Google account you added as a test user.
4. If you see a "Google hasn't verified this app" warning:
   - Click **Advanced** > **Go to gclassfetch (unsafe)**. This is normal because it is your own private project.
5. Check all permission boxes (Classroom courses, coursework, materials, announcements, and Drive).
6. Click **Continue**.

Your session is saved locally in `token.json`. You will not need to sign in again on future runs.

---

## Troubleshooting

- **Error 403: Request had insufficient authentication scopes**: Click "Account" in the top bar to re-authorize, or delete `token.json` and re-launch.
- **Access blocked: This app's request is invalid**: Check that the email you sign in with is listed under Test Users in Google Cloud Console.
- **Switching Google accounts**: Click "Account" in the top bar to sign in with a different account.
