import os
import json
import subprocess
import sys
import signal
import argparse
import uvicorn
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware

if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')

status_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sim_status.json")
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

HTML_CONTENT = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>KiCad EM & DC Simulation Dashboard</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #1e1e1e; color: #fff; margin: 0; padding: 20px; display: flex; flex-direction: column; align-items: center; }
        .card { background-color: #2d2d30; padding: 30px; border-radius: 12px; box-shadow: 0 8px 16px rgba(0,0,0,0.5); width: 100%; max-width: 700px; text-align: center; }
        h1 { color: #007acc; margin-top: 0; }
        .metric { font-size: 24px; margin: 15px 0; }
        .energy { font-weight: bold; color: #4af626; font-size: 32px; }
        .progress-bg { background-color: #3e3e42; border-radius: 8px; width: 100%; height: 30px; margin-top: 20px; overflow: hidden; position: relative; }
        .progress-bar { background-color: #007acc; height: 100%; width: 0%; transition: width 0.5s ease-in-out; }
        .progress-text { position: absolute; top: 5px; left: 50%; transform: translateX(-50%); font-size: 14px; font-weight: bold; }
        #plot-container { margin-top: 30px; display: none; background: #fff; padding: 10px; border-radius: 8px; }
        #plot-img { max-width: 100%; border-radius: 4px; }
    </style>
</head>
<body>
    <div class="card">
        <h1>☕ Electrodynamic & Static Dashboard ☕</h1>
        <div class="metric">Status: <span id="status" style="color: #ff9d00;">Waiting for data...</span></div>
        <div class="metric">Metric / Energy: <span id="energy" class="energy">0.00</span></div>
        <div class="metric" id="eta">ETA: --m --s</div>

        <div class="progress-bg">
            <div id="progress-bar" class="progress-bar"></div>
            <div id="progress-text" class="progress-text">0%</div>
        </div>

        <div id="plot-container">
            <h3 style="color: #333; margin-top: 0;" id="plot-title">Results</h3>
            <img id="plot-img" src="" alt="Simulation Plot" />
        </div>
    </div>

    <script>
        async function fetchStatus() {
            try {
                const response = await fetch('/api/status');
                const data = await response.json();

                const statusEl = document.getElementById('status');
                const energyEl = document.getElementById('energy');
                const etaEl = document.getElementById('eta');
                const pBar = document.getElementById('progress-bar');
                const pText = document.getElementById('progress-text');
                const plotContainer = document.getElementById('plot-container');
                const plotImg = document.getElementById('plot-img');
                const plotTitle = document.getElementById('plot-title');

                if (data.status === "Running" || data.status === "Starting") {
                    statusEl.innerText = data.text_status || data.status;
                    statusEl.style.color = "#4af626";
                    energyEl.innerText = (data.energy_db || 0).toFixed(2);
                    etaEl.innerText = data.eta || "ETA: --m --s";

                    let pct = data.progress_pct || 0;
                    pBar.style.width = pct + "%";
                    pText.innerText = Math.round(pct) + "%";

                    plotContainer.style.display = "none";

                } else if (data.status === "Success") {
                    statusEl.innerText = data.text_status;
                    statusEl.style.color = "#4af626";
                    pBar.style.width = "100%";
                    pText.innerText = "100%";

                    plotTitle.innerText = data.plot_title || "Simulation Results";
                    plotImg.src = "/api/plot?t=" + new Date().getTime();
                    plotContainer.style.display = "block";

                } else if (data.status === "Failed") {
                    statusEl.innerText = data.text_status;
                    statusEl.style.color = "#ff4d4d";
                } else if (data.status === "Interrupted") {
                    statusEl.innerText = data.text_status;
                    statusEl.style.color = "#ff9d00";
                } else {
                    statusEl.innerText = "Waiting for simulation to start...";
                    statusEl.style.color = "#ff9d00";
                    energyEl.innerText = "0.00";
                    etaEl.innerText = "ETA: --m --s";
                    pBar.style.width = "0%";
                    pText.innerText = "0%";
                    plotContainer.style.display = "none";
                }
            } catch (error) {
                document.getElementById('status').innerText = "Disconnected";
                document.getElementById('status').style.color = "red";
            }
        }

        setInterval(fetchStatus, 1000);
        fetchStatus();
    </script>
</body>
</html>
"""


@app.get("/")
def read_root():
    return HTMLResponse(content=HTML_CONTENT)


@app.get("/api/status")
def get_status():
    if os.path.exists(status_file_path):
        try:
            with open(status_file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    # Safe fallback while waiting for the plugin to write the first sim_status.json payload
    return {
        "status": "Waiting",
        "target_db": -30.0
    }


@app.get("/api/plot")
def get_plot():
    if os.path.exists(status_file_path):
        try:
            with open(status_file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                plot_path = data.get("plot_path", "")
                if plot_path and os.path.exists(plot_path):
                    return FileResponse(plot_path)
        except Exception:
            pass
    return HTMLResponse(content="Plot not found", status_code=404)


@app.post("/shutdown")
def shutdown_server():
    os.kill(os.getpid(), signal.SIGTERM)
    return {"message": "Server is shutting down..."}


def run_server(port=50234):
    print(f"[*] Starting Dashboard Server on http://0.0.0.0:{port}")
    try:
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    except Exception as e:
        print(f"[Dashboard] Server failed to start: {e}")
        raise


class OpenEMSServer:
    def __init__(self, port=50234):
        self.process = None
        self.port = port

    def start(self):
        startupinfo = None
        creationflags = 0
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = subprocess.CREATE_NO_WINDOW

        self.process = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--port", str(self.port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            encoding='utf-8',
            startupinfo=startupinfo,
            creationflags=creationflags
        )
        print(f"[*] Dashboard server process launched on port {self.port} (PID: {self.process.pid})")

    def stop(self):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None

    def __del__(self, ):
        self.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run EM Sim Dashboard Server")
    parser.add_argument("--port", type=int, default=50234, help="Port to listen on")
    args = parser.parse_args()

    run_server(port=args.port)