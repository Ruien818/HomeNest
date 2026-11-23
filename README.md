# HomeNest

**HomeNest** is a self-hosted, frosted-glass family hub and personal cloud suite built with **Python Flask**, **SQLite**, and **local LM Studio AI**. Designed to run smoothly on your home network (such as a Mac mini home server), HomeNest brings file storage, scheduling, tasks, news, utilities, and games together under one unified, privacy-first family portal.

---

## Core Features & Modules

### NestDrive (Family DiskStation)
* Shared and personal cloud file storage hosted right on your local network.
* Seamless integration with HomeNest's unified session authentication.

### Smart Family Calendar
* **Multi-Category Views:** Color-coded events for Family, School, Activities, Reminders, and Celebrations/Holidays.
* **Flexible Sharing:** Share events with the entire family, keep them private, or share directly with specific family members.
* **Recurring Schedules:** Support for daily, weekly, biweekly, monthly, and yearly repeating events.
* **AI Event Parser:** Type natural-language phrases (e.g., *"Guitar practice every Tuesday at 5pm"*) to auto-populate dates, times, categories, and sharing rules using local LM Studio AI with an automatic regex fallback.

### Reminders (Two-Way Calendar Sync)
* **Live Calendar Sync:** Dated reminders automatically appear on the HomeNest Calendar, and checking off a task in either app updates both immediately.
* **Subtasks & Checklists:** Break complex tasks down into checkable step-by-step subtasks with live progress tracking.
* **AI Smart Add:** Create tasks naturally with automatic priority, due date, and repeat detection.
* **1-Click Snooze & Live Alerts:** Snooze tasks to *Tomorrow* or *Next Week*, and receive live due-time banners with an optional gentle chime.

### The Daily Ledger
* Keyless, distraction-free news briefing and financial market tracker tailored for daily check-ins.

### Multilingual Dictionary & Translator
* Fast word definitions, language lookups, and multi-language translation built directly into the hub.

---

## Family Roles & AI Safety

* **Role-Based Accounts:** Supports **Parent/Admin** and **Child** family profiles under a unified frosted-glass login hub (`/`).
* **AI Safety Alerts:** Configurable content safety checks across Calendar and Reminders that block unsafe entries and log notifications for parents.
* **100% Local & Private:** Your files, schedules, and database stay on your own hardware—no external cloud subscriptions required.

---

## Tech Stack

* **Backend:** Python 3, Flask, SQLite3
* **Frontend:** HTML5, Tailwind CSS, Custom Glassmorphism UI (Light & Dark Mode), Vanilla JavaScript, Web Audio API
* **Local AI:** LM Studio (local OpenAI-compatible endpoint) with deterministic Python fallback parsing

---

## Getting Started

### 1. Prerequisites
* Python 3.10+
* *(Optional)* [LM Studio](https://lmstudio.ai/) running locally on port `1234` for AI natural-language parsing and safety checks.

### 2. Installation
```bash
# Navigate to your HomeNest directory
cd HomeNest

# Install required Python packages
pip install flask requests
```

### 3. Run the Server
```bash
python3 app.py
```

### 4. Access HomeNest
* **On the host machine:** Open `http://127.0.0.1:5000` (or your configured Flask port) in your browser.
* **From other devices on your home Wi-Fi:** Open `http://<your-server-local-ip>:5000` (e.g., `http://192.168.1.111:5000`).

---

## 📂 Project Structure

```text
HomeNest/
├── app.py                  # Main Flask application, SQLite schemas, & API routes
├── homenest.db             # Local SQLite database (auto-created on first run)
├── templates/
│   ├── index.html          # Unified login & main HomeNest app launcher hub
│   ├── calendar.html       # Interactive family calendar & AI event modal
│   ├── reminders.html      # Calendar-synced task manager, subtasks & live alerts
│   └── ...                 # NestDrive, Daily Ledger, Translator, Whiteboard & Arcade templates
└── README.md               # Project documentation
```
