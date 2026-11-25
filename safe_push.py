import subprocess as s


def run(c):
    return s.run(c, shell=True, capture_output=True, text=True)


tracked = run("git ls-files").stdout.split()
hist = run("git log --oneline -- .env").stdout.strip()
if ".env" in tracked or hist:
    raise SystemExit("STOP: .env is or was in Git. Send me this message: %s %s" % (".env" in tracked, hist))
run("git add .")
r = run('git commit -m "RetailIQ: autopilot, why engine, manage page, reports"')
print(r.stdout or r.stderr)
p = run("git push origin main")
print(p.stdout, p.stderr)
print("Tracked files:", ", ".join(tracked))