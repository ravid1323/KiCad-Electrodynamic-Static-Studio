# KiCad Electrodynamic & Static Studio
![Icon]([https://raw.githubusercontent.com/ravid1323/KiCad-Electrodynamic-Static-Studio/tree/main/resources/icon_big.png](https://raw.githubusercontent.com/ravid1323/KiCad-Electrodynamic-Static-Studio/refs/heads/main/resources/icon_big.png))

A modular Python-based plugin natively integrated into KiCad via the `kipy` IPC API. This toolchain bridges KiCad's PCB Editor with the openEMS FDTD engine and a custom C++ Modified Nodal Analysis (MNA) solver, allowing engineers to perform rigorous electrodynamic (AC/RF) and static (DC IR-drop) analyses without leaving their EDA environment.

## Key Features

* **Automated Geometry Extraction:** Natively extracts tracks, vias, pads, and polygonal zones from KiCad using the `kipy` client. Leverages `shapely` for boolean geometry operations, trace buffering, and polygon triangulation to ensure clean, meshable solids.


* **Electrodynamic Analysis (Signal Integrity):** Configures 2.5D and 3D FDTD simulations via openEMS. Automatically extracts S-parameters and handles both single-ended and mixed-mode differential pairs.
* **Static Analysis (Power Integrity & DC IR Drop):** Features a custom C++ MNA solver (`poisson_mna_solver.dll` / `libpoisson_mna_solver.so`) that solves the stationary Poisson equation. It forces constant voltage (Dirichlet) boundaries on VRMs and maps active current sinks (Neumann) directly to component pad bounding boxes.


* **Live Web Dashboard & Telemetry:** A decoupled FastAPI web server (`dashboard_server.py`) provides a live monitoring interface. Includes integrated Telegram bot support for remote status updates and mobile notifications upon simulation completion.

* **Telegram Bot support for push messages:** Create your own Telegram bot to get push messages and keep updated anywhere anytime. It will let you know when a FDTD simulation will complete, and will send you the plot of SDD11 and SDD21. Currently support one command `/status` to receive status and progress of current FDTD simulation
* **Advanced Post-Processing Suite:** Built-in tools utilizing `scikit-rf` to parse Touchstone files, perform vector fitting for SPICE subcircuit generation, and `matplotlib` to render Return/Insertion Loss graphs and TDR plots.



---

## Architecture

The project utilizes a modular, decoupled execution flow to prevent locking the KiCad GUI during heavy mathematical operations:

1. **GUI & Extraction:** A `wxPython` interface that extracts physical constraints, dynamic netclass rules, and user-defined ports, writing them to a JSON configuration payload.


2. **Headless FDTD Engine:** A standalone Python script (`run_openems.py`) that reads the JSON payload, mathematically generates the geometry, and executes the openEMS solver.


3. **Static DC Solver:** Wraps the compiled C++ library, voxelizing the PCB geometry into conductivity matrices to perform rapid DC voltage drop calculations without requiring dummy copper paths.



---

## Dependencies

To run the Simulation Studio, the following system and Python dependencies are required:

* **EDA:** KiCad >= 10.0 (with the IPC API Server enabled).
* **Simulation Engines:** `openEMS` and `CSXCAD` FDTD binaries.


* **Python Packages (`requirements.txt`):**

* `kicad-python>=0.6.0` (Required for IPC API communication)


* `wxPython~=4.2` (Required for the GUI)


* `shapely>=2.0.0` (Required for boolean geometry operations)


* `fastapi` & `uvicorn` (Required for the HTML dashboard server)


* `requests` (Required by the Telegram bot for long-polling)


* `numpy` (Required for array manipulations and mesh calculations)


* `scikit-rf` (Required to parse Touchstone files and perform vector fitting)


* `matplotlib` (Required to plot graphs and save PNG images)





*(Note: CSXCAD and openEMS are typically provided by the local openEMS system installation rather than standard PyPI packages)*.

---

## Installation

1. Locate your KiCad 10 scripting plugins directory.


* **Windows:** `C:\Users\<User>\Documents\KiCad\<Kicad_Version>\plugins\em_sim_studio\`

* **Linux:** `~/.local/share/kicad/<Kicad_Version>/plugins/em_sim_studio/`

* **macOS:** `~/Documents/KiCad/<Kicad_Version>/plugins/em_sim_studio/`



2. Create a folder called `em_sim_studio` and clone the git in the folder
   for installing only the plugin:
   ```bash
    git clone [https://github.com/ravid1323/KiCad-Electrodynamic-Static-Studio.git](https://github.com/ravid1323/KiCad-Electrodynamic-Static-Studio.git)
    ```
   for installing the plugin alongside the examples:
   ```bash
    git clone --recurse-submodules [https://github.com/ravid1323/KiCad-Electrodynamic-Static-Studio.git](https://github.com/ravid1323/KiCad-Electrodynamic-Static-Studio.git)
    ```
4. KiCad will automatically scan this directory on startup to map your toolbar icon to the UI.


5. Open KiCad, go to **Preferences > Plugins**, and ensure the **IPC API server** is enabled.
6. Install the necessary Python dependencies into the environment KiCad uses.
   ```bash
    python -m pip install -r requirements.txt
    pip3 install -r requirements.txt
    wsl pip3 install -r requirements.txt
    ```
7. Restart KiCad.

---

## Usage Workflow

1. **Launch:** Open the KiCad PCB Editor and click the Electrodynamic & Static Studio icon in the main toolbar.
2. **Net Extraction:** In the *Net Extraction* tab, search and select the high-speed traces or power planes you wish to simulate.
3. **Stackup Sync:** Navigate to the *Stackup & Sim Setup* tab and click "Sync Stackup via S-Expression" to automatically pull your substrate thicknesses and dielectric constants from the board.
4. **Define Ports / Sinks:**
* For **Electrodynamic Analysis (SI)**, use the *Ports Configuration* tab to assign MSL or Lumped ports to specific component pads.
* For **Static Analysis (PI)**, use the *DC Analysis* tab to assign Voltage Regulator (VRM) sources and active load current sinks. The solver automatically distributes current directly into the nodes inside that pad.




5. **Export & Mesh:** Click **Export to JSON** to compile your settings, then click **Calculate Mesh** to evaluate the estimated FDTD cell count.
6. **Simulate:** Click **Run Simulation**. You can track the progress live by visiting `http://localhost:50234` in your web browser.
7. **Post-Process:** Once complete, use the *Post-Processing* tab to generate S-Parameter plots, synthesize SPICE models, or render 2D/3D DC potential maps.

---

## Automated Report Generator (PowerPoint)

The plugin features a built-in report generator to seamlessly compile simulation results, impedance plots, and 3D geometry views into a professional .pptx presentation.

Key Features & Workflow:
1. One-Click Integration: Inside the Post-Processing tab, use the "Add Plot to Report" button to instantly queue your active SI/PI plots.   
2. External Image Support: In the Report Generator tab, you can queue external images by selecting "Add Photo from File" or by copying any image to your OS clipboard and clicking "Add Photo from Clipboard".   
3. Smart Template Formatting: Click "Generate PPTX Report" to automatically build the presentation using the resources/template.pptx file.   
4. Dynamic Metadata: The generator automatically populates the main title slide with your KiCad project name, the exact timestamp of generation.   
5. Auto-Centering: Each queued image is given its own dedicated slide and mathematically centered for a clean, uniform layout.

---

## Screenshots

![Net List Select](https://raw.githubusercontent.com/ravid1323/KiCad-Electrodynamic-Static-Studio-Examples/main/Screenshots/net_list_select.png)
![Stackup Sync](https://raw.githubusercontent.com/ravid1323/KiCad-Electrodynamic-Static-Studio-Examples/main/Screenshots/stackup_sync.png)
![Sim Setting](https://raw.githubusercontent.com/ravid1323/KiCad-Electrodynamic-Static-Studio-Examples/main/Screenshots/sim_setting.png)
![Mesh Setting](https://raw.githubusercontent.com/ravid1323/KiCad-Electrodynamic-Static-Studio-Examples/main/Screenshots/mesh_setting.png)
![Component Manager](https://raw.githubusercontent.com/ravid1323/KiCad-Electrodynamic-Static-Studio-Examples/main/Screenshots/component_manager.png)
![Port Config](https://raw.githubusercontent.com/ravid1323/KiCad-Electrodynamic-Static-Studio-Examples/main/Screenshots/port_config.png)
![DC Network Config](https://raw.githubusercontent.com/ravid1323/KiCad-Electrodynamic-Static-Studio-Examples/main/Screenshots/dc_network_config.png)
![Post Processing](https://raw.githubusercontent.com/ravid1323/KiCad-Electrodynamic-Static-Studio-Examples/main/Screenshots/post_processing.png)
![Report Generator](https://raw.githubusercontent.com/ravid1323/KiCad-Electrodynamic-Static-Studio-Examples/main/Screenshots/report_generator.png)
