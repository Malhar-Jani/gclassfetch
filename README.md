# gclassfetch

A fast, private tool to download all your Google Classroom courses, lecture notes, assignments, and attachments into clean local folders.

Features a lightweight local web dashboard, parallel downloads, Google Docs export to Office/PDF, and an offline HTML index viewer.

---

## Why this exists

Google Classroom doesn't have a button to download all course files. Professors often upload lecture notes, slides, and study material across different posts, announcements, and Drive folders. Manually clicking through dozens of items before a semester ends is tedious.

`gclassfetch` connects to the Google Classroom and Drive APIs to fetch everything at once:
- Course materials, lecture notes, and slides
- Coursework and assignment attachments
- Post descriptions and announcements (saved as text digests)
- Google Drive files and folder links shared in post descriptions
- Auto-exports Google Docs, Sheets, Slides, and Drawings to `.docx`, `.xlsx`, `.pptx`, and `.pdf`
- Generates a local `index.html` file in your download directory to browse all files offline

---

## Folder layout

Files are organized by subject, topic, and post title:

```text
Classroom/
├── index.html                   (offline browser viewer)
├── Machine Learning/
│   ├── General/
│   │   └── Syllabus/
│   │       ├── syllabus.pdf
│   │       └── links.txt
│   ├── Week 1 - Linear Regression/
│   │   ├── Lecture Notes/
│   │   │   ├── slides.pptx
│   │   │   └── practice.pdf
│   │   └── Assignment 1/
│   │       ├── homework.docx
│   │       └── description.txt
│   └── _announcements/
│       ├── announcements.txt
│       └── exam_date.pdf
└── Data Structures/
    └── Trees/
        ├── lecture_tree.pdf
        └── links.txt
```

---

## Requirements

- Python 3.8 or higher
- A Google account enrolled in Classroom classes

---

## Quick start

### 1. Clone and install dependencies

```bash
git clone https://github.com/Malhar-Jani/gclassfetch.git
cd gclassfetch
pip install -r requirements.txt
```

### 2. Add your Google OAuth credentials

To connect to your Classroom account, you need a free OAuth client ID from Google Cloud (takes about 3 minutes):

1. Go to [Google Cloud Console](https://console.cloud.google.com/) and create a project.
2. In **APIs & Services > Library**, enable:
   - Google Classroom API
   - Google Drive API
3. In **OAuth consent screen**:
   - Set user type to External.
   - Enter your email and add your email under **Test users**.
4. In **Credentials**:
   - Click **Create Credentials > OAuth client ID**.
   - Select **Desktop app**.
   - Download the JSON file, rename it to `credentials.json`, and place it in the project root folder.

For a full step-by-step walkthrough, see [SETUP.md](SETUP.md).

### 3. Run

- **Windows**: Double-click `run.bat` or run:
  ```cmd
  python app.py
  ```
- **macOS / Linux**:
  ```bash
  chmod +x run.sh
  ./run.sh
  ```

Your browser will open `http://localhost:5328`. Sign in with your Google account, select your courses, and click **Fetch ALL Courses**.

---

## CLI usage

You can also run `gclassfetch` from the terminal without opening a browser:

```bash
# Download all active courses to a folder
python app.py --cli --all --dest "D:\School\Sem-III" --workers 8

# Download specific courses by ID
python app.py --cli --courses "123456789,987654321" --dest "./downloads"
```

Available flags:
- `--cli`: Run in headless command line mode
- `--all`: Download all active courses
- `--courses <ids>`: Comma-separated course IDs
- `--dest <path>`: Download destination directory
- `--workers <n>`: Parallel download threads (default: 8)
- `--port <n>`: Local web server port (default: 5328)
- `--no-browser`: Do not auto-launch browser on startup

---

## Desktop shortcut (Windows)

Double-click `setup_desktop_shortcut.bat` to add a `GClassFetch` shortcut to your Windows Desktop.

---

## Privacy and security

- Runs 100% locally on your machine (`localhost:5328`).
- Direct connection between your computer and Google's official APIs.
- No telemetry, no external servers, no third-party tracking.
- `.gitignore` is set up to never commit `credentials.json`, `token.json`, `config.json`, or downloaded files.

---

## License

MIT License. See [LICENSE](LICENSE) for details.
