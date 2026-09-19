# GitHub + Vercel deployment

From this folder (the folder containing `app.py`, `requirements.txt`, `vercel.json`, and `api/`):

```powershell
git init
git add .
git commit -m "Prepare project for Vercel"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPOSITORY.git
git push -u origin main
```

Then import the GitHub repository in Vercel.

Vercel settings:
- Root Directory: `.`
- Framework: leave auto-detected / do not select Next.js
- Environment variable: `SECRET_KEY` (use a random value)

Expected root:
- `api/index.py`
- `app.py`
- `requirements.txt`
- `vercel.json`
- `templates/`
- `static/`
