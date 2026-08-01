import json, requests

base = "http://127.0.0.1:8000/api/v1"
s = requests.Session()
r = s.post(f"{base}/auth/session", json={})
t = r.json()["access_token"]
h = {"Authorization": f"Bearer {t}"}

# List repos
repos = s.get(f"{base}/repositories", headers=h).json()
print("REPOS:", json.dumps(repos, indent=2)[:600], "\n")

# List conversations
conv = s.get(f"{base}/conversations", headers=h)
if conv.status_code == 200:
    print("CONVERSATIONS:", json.dumps(conv.json(), indent=2)[:600])
else:
    print("Conversations endpoint:", conv.status_code, conv.text[:200])

# List indexing-jobs
ij = s.get(f"{base}/indexing-jobs", headers=h)
if ij.status_code == 200:
    print("\nINDEXING-JOBS:", json.dumps(ij.json(), indent=2)[:600])
else:
    print("indexing-jobs endpoint:", ij.status_code, ij.text[:200])
