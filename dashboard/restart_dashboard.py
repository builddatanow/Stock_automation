"""Kill the waitress dashboard process and restart it."""
import psutil, subprocess, time, os, sys

DASHBOARD_DIR = r"C:\Users\Administrator\Desktop\projects\dashboard"
PYTHON = r"C:\Program Files\Python311\python.exe"

# Kill existing waitress/app.py process
for p in psutil.process_iter(['pid', 'name', 'cmdline']):
    try:
        cmd = ' '.join(p.info['cmdline'] or [])
        if ('waitress' in cmd and 'app:app' in cmd) or ('app.py' in cmd and 'watchdog' not in cmd and 'restart' not in cmd):
            print(f"Killing PID {p.info['pid']}: {cmd[:60]}")
            p.kill()
    except Exception as e:
        print(f"  skip: {e}")

time.sleep(2)
print("Starting new waitress process...")
log = open(os.path.join(DASHBOARD_DIR, "dashboard.log"), "a")
proc = subprocess.Popen(
    [PYTHON, "-m", "waitress", "--host=0.0.0.0", "--port=5000", "--threads=4", "app:app"],
    cwd=DASHBOARD_DIR,
    stdout=log, stderr=log,
    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
)
print(f"Started PID {proc.pid}")
time.sleep(3)
print("Done.")
