# HomeNest DSM

**Frosted Glassmorphism Family Cloud Drive, Desktop Environment & Home DiskStation**

HomeNest is a self-hosted, single-machine web DiskStation built with **Python**, **Flask**, and **SQLite**. Styled with a translucent frosted-glass desktop interface, it combines a visual file manager (**NestDrive**), an interactive **Desktop** workspace, a **Working Web Terminal** with live filesystem and internet tools, a **Trash Bin**, and a **Multi-Engine Content Safety Scanner** powered by Python heuristics and local **LM Studio** inference.

---

## Features

### 1. HomeNest Desktop
* **Spatial Workspace:** Pin frequently used files and color-coded folders directly onto a freeform desktop canvas.
* **Drag-and-Drop Organization:** Drag files directly onto desktop folders to move them into NestDrive directories, or drag items onto the **Desktop Trash Bin** to soft-delete them.
* **Lasso Multi-Select:** Click and drag across the desktop canvas to marquee-select multiple shortcuts at once.
* **Direct Desktop Uploads:** Drag files from your computer onto the desktop to upload, scan, and pin them automatically.

### 2. NestDrive (Family File Station)
* **Hierarchical Folders & Custom Color Tags:** Organize family documents into nested folders with custom accent colors and breadcrumb navigation.
* **Live Rightbar Inspector & Full-Screen Previewer:** Click any file to inspect metadata, safety reports, and live inline previews (images, PDFs, audio, video, and source code/text files), or expand into a full-screen viewer.
* **Context Menus & Bulk Actions:** Right-click any row for quick actions (Preview, Full Screen, Send to Desktop, Download, Rename, Move to Trash) or lasso-select rows for batch operations.

### 3. HomeNest Shell (Working Terminal)
* **Live NestDrive Filesystem Sync:** Navigate and manipulate your virtual cloud directories (`ls`, `cd`, `pwd`, `tree`, `mkdir`, `touch`, `echo > / >>`, `cat`, `head`, `tail`, `mv`, `rm`, `find`, `grep`, `open`, `desktop`). Changes sync immediately with the UI.
* **Real Internet & Network Commands:** Execute live host network diagnostics and utilities (`curl`, `wget`, `ping`, `nslookup`, `dig`, `whois`, `traceroute` / `tracert`, `ifconfig` / `ipconfig`, `python3`).
* **Direct Web-to-Drive Downloads:** Running `wget <url>` or `curl <url> -o <filename>` downloads remote files directly into your current NestDrive folder and runs them through the safety scanner.
* **Shell Ergonomics:** Includes `Up`/`Down` arrow command history, `Tab` auto-completion for commands and filenames, and one-click quick-run chips.

### 4. Multi-Engine Content Safety & Family Roles
* **Role-Based Accounts:**
  * **Kid:** Sensitive content scanning is locked **ON**; flagged uploads notify both the user and all Parent accounts.
  * **Adult:** Configurable safety scanning toggle in the Safety settings panel.
  * **Parent:** Full visibility across family files, folders, trash, and real-time safety alerts.
* **Three-Layer Inspection Pipeline:**
  1. **Python Threat & PII Engine:** Scans text files and `.docx` archives (via XML extraction) for threats, self-harm keywords, exposed credentials/API keys, SSNs, and payment card patterns.
  2. **Script & Executable Heuristics:** Flags potentially risky executable extensions (`.exe`, `.bat`, `.sh`, `.vbs`, `.ps1`, `.cmd`).
  3. **LM Studio Local AI (Optional):** Sends text excerpts to a local OpenAI-compatible endpoint (`http://localhost:1234/v1/chat/completions`) for semantic safety classification with zero cloud data sharing.

### 5. Trash Bin & Notification Center
* **Safe Soft-Delete:** Deleted files and folders move to the Trash Bin with recursive folder restoration or permanent purging.
* **Slide-Out Notification Center:** Real-time alerts and system activity log accessible from the top bar or via **`Alt + N`**.

---

## Project Structure

```text
HomeNest/
├── app.py                  # Flask backend, SQLite auto-migrations, scanner & terminal engine
├── homenest.db             # Auto-generated SQLite database (users, folders, files, desktop, alerts)
├── files/                  # Physical storage directory for uploaded files
└── templates/
    └── index.html          # Complete glassmorphism desktop, NestDrive, terminal & preview UI
```

---

## Quick Start

### 1. Prerequisites
* **Python 3.9+**
* *(Optional)* **LM Studio** running a local chat model with the local server started on port `1234` for AI-assisted content scanning.

### 2. Installation
Clone the repository and install the required Python packages:

```bash
git clone [https://github.com/](https://github.com/)<Ruien818>/homenest.git
cd homenest
pip install flask werkzeug requests
```

### 3. Run the Server
```bash
python app.py
```

Open your browser and navigate to:
* **Local:** `http://localhost:5000`
* **Local Network (LAN):** `http://<your-local-ip>:8686`

Create your first **Parent** or **Kid** account on the login screen to initialize your workspace.

---

## Terminal Command Reference

### NestDrive File & Folder Commands
| Command | Description |
| :--- | :--- |
| `ls [-la] [path]` | List folders and files in the current or target directory |
| `cd <folder\|..\|/>` | Change current NestDrive directory |
| `pwd` | Print current virtual path |
| `tree` | Display a recursive tree of all folders and files |
| `mkdir <name>` | Create a new folder |
| `touch <file>` | Create an empty file |
| `echo "text" > <file>` | Write (`>`) or append (`>>`) text to a file |
| `cat` / `head` / `tail <file>` | Read file contents (supports `.txt`, `.py`, `.md`, `.docx`, etc.) |
| `mv <src> <dst>` | Rename a file/folder or move it into another folder |
| `rm <name>` | Move a file or folder to the Trash Bin |
| `find <query>` | Search folders and files by name |
| `grep <text>` | Search for matching lines inside files in the current folder |
| `open <file>` | Launch a file directly in the Full-Screen Previewer |
| `desktop <name>` | Pin a file or folder shortcut to the Desktop |
| `clear` | Clear the terminal screen |

### Internet & System Commands
| Command | Description |
| :--- | :--- |
| `curl <url>` | Fetch HTTP responses or headers (`curl -I <url>`) |
| `curl <url> -o <file>` | Download a remote file into the current NestDrive folder |
| `wget <url>` | Download a URL directly into the current NestDrive folder |
| `ping <host>` | Test network latency (automatically bounded to 4 packets) |
| `nslookup` / `dig <domain>` | Query live DNS records |
| `whois <domain>` | Look up domain registration details |
| `traceroute <host>` | Trace network hops (automatically maps to `tracert` on Windows) |
| `ifconfig` / `ipconfig` | Display local network interface configuration |
| `python3 -c "..."` | Execute Python commands or scripts |

---

## Configuration & Security Notes

* **Secret Key:** Before running HomeNest on a shared local network, replace `app.secret_key = "homenest-family-secret-key-change-me"` in `app.py` with a strong random string.
* **Host Shell Access:** The built-in terminal executes real host OS commands (`ping`, `curl`, `python3`, etc.) inside the `HomeNest/files` working directory for authenticated users. Intended for trusted home/family LAN deployments.
* **Local AI Endpoint:** To change the LM Studio server address or port, update `LM_STUDIO_URL` near the top of `app.py`. If LM Studio is not running, HomeNest automatically falls back to its Python Regex and Heuristic scanners without interruption.
