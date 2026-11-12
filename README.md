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
- Searches the offline library by course or filename
- Verifies existing downloads using source metadata and content checksums
- Reports inaccessible files, incomplete scans, and online-only resources separately
- Includes archived courses when you choose **Include archived**

---

## Folder layout

Each course gets one folder, with its files directly inside. Shared Drive folders are scanned fully without copying their folder nesting.

```text
Classroom/
├── index.html
├── download-report.json
├── Machine Learning/
│   ├── syllabus.pdf
│   ├── lecture_01.pptx
│   ├── homework.docx
│   ├── Classroom notes.txt
│   ├── Classroom announcements.txt
│   └── Classroom links.txt
└── Data Structures/
    ├── trees.pdf
    └── practice.pdf
```

Notes, announcements, and links are combined per course, rather than creating folders for individual posts. The same Drive attachment is saved once per course. Different files sharing a filename are kept with a numbered suffix.

When a course already contains the older nested layout, the app moves its files directly into the course folder. It preserves distinct files, merges the old text digests, and removes only empty directories. Original text and file records plus a journal of attachment moves are kept in the hidden `.gclassfetch-backups` folder. Download bookkeeping is hidden in Windows Explorer.

---

## Requirements

- Python 3.9 or higher
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

Your browser will open `http://localhost:5328`. Sign in with your Google account, select your courses, choose a destination, and click **Download courses**. Detailed options are under **Download settings**.

When it finishes, the summary separates newly saved files, verified existing files, online links, and anything that needs attention. **Retry affected courses** checks those courses again and reuses verified downloads. A `download-report.json` is saved in the destination after each run. Reports include local paths and course information; keep them private.

Open `index.html` in your download folder to browse each course’s files without running the app. Search also finds filenames and file extensions. Repeated runs do not repeat the same description or link entry. Attachments stay in the course folder, even when posts share a title. Different files with the same filename are kept distinct.

Existing downloads made by older versions are reused when their checksum matches Google's original. If an old file cannot be verified, it is preserved and a new copy is saved. Changed documents are also saved as new copies so an earlier download is never overwritten. The hidden `.gclassfetch.json` in each course folder tracks verified files; keep it with your downloads.

Google Forms, videos, websites, and other online resources are recorded as links rather than offline copies. Files deleted by their owner, inaccessible to your account, blocked from downloading, or beyond Google's export limits appear in the review. The app cannot bypass those restrictions.

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

## Development checks

Run the download and route regression checks with `python -m unittest discover -s tests -v`.

For browser interaction checks, install Playwright and Chromium in your development environment, run the local app, then run `node tests/ui_smoke.cjs`. If Playwright is installed elsewhere, set `PLAYWRIGHT_MODULE` to its module path. The browser checks use API fixtures, test 360px, 768px, and 1440px layouts, and do not download course content or change saved settings.
