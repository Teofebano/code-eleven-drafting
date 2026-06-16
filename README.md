# Code Eleven — Draft Day

Real-time football draft app for ITB Informatics & STI alumni internal tournaments.

## Stack
- **Backend**: Python + FastAPI + WebSockets
- **Database**: SQLite (persists across restarts via Railway volume)
- **Frontend**: Vanilla JS, no build step needed

---

## Deploy to Railway

### 1. Push to GitHub
```bash
git init
git add .
git commit -m "init code-eleven-draft"
git remote add origin https://github.com/YOUR_USERNAME/code-eleven-draft
git push -u origin main
```

### 2. Deploy on Railway
1. Go to https://railway.app → **New Project** → **Deploy from GitHub repo**
2. Select your repo
3. Railway auto-detects Python and runs the Procfile

### 3. Set custom domain with required wording
1. In Railway project → **Settings** → **Networking** → **Custom Domain**
2. Or use the Railway-generated domain and set an alias like `code-eleven-draft.up.railway.app`
3. Under **Generate Domain** — Railway gives you a subdomain; name the service `code-eleven-draft` for a URL like `code-eleven-draft.up.railway.app`

### 4. Set environment variables (optional but recommended)
In Railway → Variables:
```
ADMIN_PASSWORD=your_secure_password
SECRET_KEY=your_random_secret_key_here
```

### 5. Add a persistent volume (keeps SQLite data across deploys)
1. Railway → your service → **Add Volume**
2. Mount path: `/app/data`

Without a volume, data resets on each deploy — fine for testing, not for event day.

---

## How to name the service for the URL
When creating the Railway service, name it exactly: `code-eleven-draft`
This gives you: `https://code-eleven-draft.up.railway.app`

---

## Default Credentials
- **Admin**: username `admin`, password `codeeleven2025`
- Change these immediately via Admin Panel → Settings

---

## How to run a draft

### Setup (before the event)
1. Log in as admin at `/`
2. **Captains tab**: Add each captain + set their password
3. **Players tab**: Add players manually or upload CSV
   - CSV columns: `name`, `batch_year`, `position` (flexible naming)
4. **Questions tab**: Add at least 3–5 football trivia questions

### Draft day flow
1. Share the URL with captains — they log in on their own devices
2. Admin → **Draft Control** → **▶ Start Next Round**
3. MCQ appears on all captain screens simultaneously
4. 15-second timer — captains tap their answer (sounds play!)
5. All correct answers sorted by speed → draft order locked
6. Admin can drag-reorder before confirming
7. Fastest correct answerer picks first — picks cycle through the order
8. Once all players in the group are picked → admin starts next round
9. After 3 rounds → **End Draft** → final teams displayed

### Batch groups
| Group | Years |
|-------|-------|
| Veterans | ≤ 2004 |
| Mid | 2005 – 2018 |
| Young | > 2018 |

### CSV format
```csv
name,batch_year,position
Budi Santoso,2012,CM
Andi Pratama,2001,ST
Reza Firmansyah,2019,GK
```

Accepted position values: GK, CB, RB, LB, CDM, CM, CAM, RW, LW, ST, CF

---

## Local development
```bash
pip install -r requirements.txt
uvicorn main:app --reload
# open http://localhost:8000
```
