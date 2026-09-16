import os
import sys

# Add parent directory to path so server.py can be imported
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import app

# Vercel entrypoint
if __name__ == "__main__":
    app.run()
