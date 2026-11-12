import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# hermes-agent tools/ package
for p in ["/home/michael/.hermes/installs/55dbd0577755667c/environments/de7a5629ca5649fcb179f5140591457e/workspace", "/home/michael/.hermes/hermes-agent"]:
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
