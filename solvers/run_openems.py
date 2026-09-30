import os, sys

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

import re
import json
import numpy as np
import argparse
import subprocess
import multiprocessing
import skrf as rf
from skrf.vectorFitting import VectorFitting
try:
    from matplotlib import pyplot as plt
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False
    print("[!] Warning: 'matplotlib' library is missing! Run: pip install matplotlib")

try:
    from shapely.geometry import Polygon, LineString, Point
    from shapely.ops import triangulate, unary_union
    from shapely.geometry import box
    from shapely.affinity import scale

    HAS_SHAPELY = True
except ImportError:
    HAS_SHAPELY = False
    print("[!] Warning: 'shapely' library is missing! Run: pip install shapely")

from core.csx_handler import CSXGeometryLogger, ContinuousStructure, AppCSXCAD_BIN
import openEMS
from openEMS import ports
from core.model_builder import *

# ==========================================
# Touchstone Exporters
# ==========================================

def write_touchstone_s1p(filepath, freq_hz, s11, z0=50.0):
    freq_ghz = freq_hz / 1e9
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write("! Touchstone 1-Port S-parameter file\n")
        f.write(f"# GHz S RI R {z0}\n")
        for i in range(len(freq_hz)):
            f.write(f"{freq_ghz[i]:14.6e} {np.real(s11[i]):14.6e} {np.imag(s11[i]):14.6e}\n")
    print(f"[*] Exported Touchstone .s1p -> {filepath}")

def write_touchstone_s2p(filepath, freq_hz, s11, s21, z0=50.0):
    s12 = s21
    s22 = s11
    freq_ghz = freq_hz / 1e9
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write("! Touchstone 2-Port S-parameter file\n")
        f.write(f"# GHz S RI R {z0}\n")
        f.write("! Freq(GHz) Re(S11) Im(S11) Re(S21) Im(S21) Re(S12) Im(S12) Re(S22) Im(S22)\n")
        for i in range(len(freq_hz)):
            f.write(f"{freq_ghz[i]:14.6e} {np.real(s11[i]):14.6e} {np.imag(s11[i]):14.6e} "
                    f"{np.real(s21[i]):14.6e} {np.imag(s21[i]):14.6e} "
                    f"{np.real(s12[i]):14.6e} {np.imag(s12[i]):14.6e} "
                    f"{np.real(s22[i]):14.6e} {np.imag(s22[i]):14.6e}\n")
    print(f"[*] Exported Touchstone .s2p -> {filepath}")

def export_network_to_touchstone(filepath, freq_hz, s_matrix_ports_first, z0=50.0):
    """
    Generic Touchstone writer using scikit-rf.
    Accepts s_matrix of shape (num_ports, num_ports, num_freqs).
    """
    # skrf expects s array shape: (num_freqs, num_ports, num_ports)
    s_transposed = np.moveaxis(s_matrix_ports_first, -1, 0)
    freq = rf.Frequency.from_f(freq_hz, unit='hz')
    net = rf.Network(frequency=freq, s=s_transposed, z0=z0)

    net.write_touchstone(filepath)
    print(f"[*] Exported {net.nports}-Port Touchstone -> {filepath}")
    return net

def plot_touchstone(filepath):
    """Reads any Touchstone file (.s1p, .s2p, .s4p, .sNp) and plots S-parameters."""
    if not HAS_MATPLOTLIB or not os.path.exists(filepath):
        return

    print(f"[*] Parsing and plotting Touchstone file: {filepath}")
    try:
        net = rf.Network(filepath)
        plt.figure(figsize=(10, 6))

        if net.nports == 1:
            net.plot_s_db(m=0, n=0, label='S11 (Return Loss)')
        elif net.nports == 2:
            net.plot_s_db(m=0, n=0, label='S11 (Return Loss)', color='red')
            net.plot_s_db(m=1, n=0, label='S21 (Insertion Loss)', color='blue')
        elif net.nports == 4:
            # Rigorous Mixed-Mode conversion (2 differential pairs)
            net_mm = net.copy()
            net_mm.se2gmm(p=2)
            net_mm.plot_s_db(m=0, n=0, label='Sdd11 (Diff Return Loss)', color='red')
            net_mm.plot_s_db(m=1, n=0, label='Sdd21 (Diff Insertion Loss)', color='blue')
            net_mm.plot_s_db(m=2, n=2, label='Scc11 (Comm Return Loss)', color='orange', linestyle='--')
        else:
            net.plot_s_db()

        plt.title(f'S-Parameters - {os.path.basename(filepath)}')
        plt.grid(True, which='both', linestyle='--', linewidth=0.5)
        plt.legend(loc='best')
        plt.tight_layout()

        out_png = filepath + ".png"
        plt.savefig(out_png)
        print(f"[*] Plot successfully saved to {out_png}")
        plt.show()
    except Exception as e:
        print(f"[!] Failed to parse Touchstone file via scikit-rf: {e}")

def convert_s_params_to_spice(touchstone_path, num_poles=13):
    """
    Converts a Touchstone S-parameter file into a SPICE subcircuit (.cir)
    using Vector Fitting, dynamically handling port/pin assignment.
    """
    if not os.path.exists(touchstone_path):
        print(f"[!] Error: File not found -> {touchstone_path}")
        return

    print(f"[*] Loading network from {touchstone_path}...")
    net = rf.Network(touchstone_path)
    num_ports = net.number_of_ports
    print(f"[*] Detected {num_ports}-Port Network.")

    # Initialize the Vector Fitting algorithm
    vf = VectorFitting(net)

    # Perform the mathematical fit
    # Note: num_poles controls the accuracy. Complex curves (like resonances) require more poles.
    print(f"[*] Performing Vector Fitting with {num_poles} complex pole pairs...")
    vf.vector_fit(n_poles_real=1, n_poles_cmplx=num_poles)

    # Enforce Passivity (CRITICAL for SPICE)
    # If the model is not passive, the SPICE transient solver will blow up to infinity.
    is_passive = vf.is_passive()
    if not is_passive:
        print("[*] Model is non-passive. Enforcing passivity for SPICE stability...")
        vf.passivity_enforce()
    else:
        print("[*] Model is inherently passive. No enforcement needed.")

    # Generate the SPICE subcircuit file
    base_name = os.path.splitext(os.path.basename(touchstone_path))[0]
    output_dir = os.path.dirname(touchstone_path)
    spice_path = os.path.join(output_dir, f"{base_name}_model.cir")

    # This function automatically assigns the pins: Port 1 -> p1, Port 2 -> p2, + Ground (ref)
    vf.write_spice_subcircuit_s(spice_path)

    print(f"[*] SPICE Subcircuit successfully exported -> {spice_path}")

    # 5. Output the subcircuit header so you know exactly how to wire it in Ngspice!
    pins = " ".join([f"p{i + 1}" for i in range(num_ports)])
    print(f"[*] SPICE Definition: .subckt {base_name}_model {pins} ref")

def remove_abort_file(path):
    abort_path = os.path.join(path, "ABORT")
    if os.path.exists(abort_path):
        try:
            os.remove(abort_path)
        except Exception:
            pass

def run_single_port_worker(json_path, port_num, total_ports):
    """
    Isolated worker function to run the FDTD solver for a single port.
    Spawns an independent engine, creates a dedicated sub-folder, and executes the math.
    """
    print(f"[*] Executing FDTD Run {port_num} of {total_ports}")

    # Initialize an independent engine
    engine = OpenEMSEngine(json_path)

    # Create a mathematically isolated output folder
    port_dir = os.path.join(engine.config.sim_dir, f"run_port_{port_num}")
    os.makedirs(port_dir, exist_ok=True)
    engine.config.sim_dir = port_dir

    # Build the 3D model, exciting ONLY this specific port
    engine.build_model(active_port=port_num)

    # Run the C++ solver
    remove_abort_file(port_dir)
    engine.FDTD.Run(port_dir, cleanup=True)
    remove_abort_file(port_dir)

    # Return the path so the master thread knows where to find the raw voltage arrays
    return port_num, port_dir





# ==========================================
# Port Management
# ==========================================
class PortManager:
    """
    Creates physical lumped/MSL ports mapped perfectly to KiCad pad boundaries.
    Utilizes priority=10000 to safely overwrite PEC and AddEdges2Grid for perfect meshing.
    """

    def __init__(self, CSX, config, geometry):
        self.CSX = CSX
        self.config = config
        self.ports_list = []
        self.geometry = geometry
        self.port_geometry = []
        self.validation_errors = []

    def _find_pad(self, kiid):
        """Locates the raw pad dictionary from the KiCad extraction using its unique UUID."""
        if not kiid: return None
        for pad in self.config.all_pads:
            if pad.get("kiid") == kiid: return pad
        return None

    def register_port_mesh_keys(self):
        """Pre-registers all port pad boundaries into key points before meshing."""
        manual_ports = self.config.raw_data.get("manual_ports", {})

        for p_key, p_info in manual_ports.items():
            sig_layer = p_info.get("signal_layer", "F.Cu")
            ref_layer = p_info.get("reference_layer", "In1.Cu")

            sig_data = self.config.layer_z.get(sig_layer, {})
            ref_data = self.config.layer_z.get(ref_layer, {})

            z_sig = sig_data.get("z", 0.0)
            z_ref = ref_data.get("z", 0.0)
            self.geometry.key_z_points.extend([z_sig, z_ref])

            for term_key in ["positive_terminal", "negative_terminal"]:
                term = p_info.get(term_key, {})
                pad = self._find_pad(term.get("kiid", ""))
                if pad:
                    px, py = to_csx(pad["x"], pad["y"], self.config.center_x, self.config.center_y)
                    pad_shapes = pad.get("pad_shapes", [])
                    if pad_shapes:
                        r_x = pad_shapes[0].get("size_x_mm", 1.0) / 2.0
                        r_y = pad_shapes[0].get("size_y_mm", 1.0) / 2.0
                    else:
                        r_x, r_y = 0.5, 0.5

                    p_type_clean = str(p_info.get("type", "Lumped")).lower()
                    if "msl" in p_type_clean or "microstrip" in p_type_clean:
                        prop_dir = term.get("prop_dir", p_info.get("current_prop_dir", "x"))
                        dir_sign = int(term.get("direction_sign", p_info.get("current_direction_sign", 1)))
                        msl_len = float(term.get("msl_length", p_info.get("msl_length", 50.0)))
                        feed_shift = float(term.get("feed_shift", p_info.get("feed_shift", 4.48)))
                        meas_shift = float(term.get("meas_plane_shift", p_info.get("meas_plane_shift", 16.67)))

                        if prop_dir == 'x':
                            p_start_x = px - r_x if dir_sign == 1 else px + r_x
                            p_stop_x = p_start_x + (msl_len * dir_sign)
                            self.geometry.key_x_points.extend([p_start_x, p_stop_x])
                            self.geometry.key_y_points.extend([py - r_y, py + r_y])
                        else:
                            p_start_y = py - r_y if dir_sign == 1 else py + r_y
                            p_stop_y = p_start_y + (msl_len * dir_sign)
                            self.geometry.key_x_points.extend([px - r_x, px + r_x])
                            self.geometry.key_y_points.extend([p_start_y, p_stop_y])
                    else:
                        self.geometry.key_x_points.extend([px - r_x, px + r_x])
                        self.geometry.key_y_points.extend([py - r_y, py + r_y])

    def _create_openems_port(
            self, FDTD, port_nr, p_start, p_stop, p_dir, p_type, excite_val, R=50.0, port_params=None
    ):
        if port_params is None: port_params = {}
        p_type_clean = str(p_type).lower().strip()
        sig_layer = port_params.get("signal_layer", "F.Cu")
        metal_prop = self.geometry.layer_metals.get(sig_layer)

        prop_dir = port_params.get("current_prop_dir", "x")
        exc_dir = p_dir

        try:
            if "waveguide" in p_type_clean or "rect" in p_type_clean:
                dx = abs(p_stop[0] - p_start[0])
                dy = abs(p_stop[1] - p_start[1])
                dz = abs(p_stop[2] - p_start[2])

                a, b = (dy, dz) if prop_dir == 'x' else ((dx, dz) if prop_dir == 'y' else (dx, dy))
                a = max(a, self.config.min_cell_size_mm)
                b = max(b, self.config.min_cell_size_mm)

                port = FDTD.AddRectWaveGuidePort(
                    port_nr,
                    p_start,
                    p_stop,
                    prop_dir,
                    a=a,
                    b=b,
                    mode_name="TE10",
                    excite=excite_val,
                    priority=10000
                )
                print(f"[*] Registered Port {port_nr} as RectWGPort (a={a:.3f}, b={b:.3f})")


            elif "coax" in p_type_clean:
                port = FDTD.AddCoaxPort(port_nr, p_start, p_stop, p_dir, excite=excite_val, priority=10000)
                print(f"[*] Registered Port {port_nr} as CoaxPort")


            elif "coplanar" in p_type_clean or "cpw" in p_type_clean:
                port = FDTD.AddCPWPort(port_nr, p_start, p_stop, p_dir, excite=excite_val, priority=10000)
                print(f"[*] Registered Port {port_nr} as CPWPort")

            elif "microstrip" in p_type_clean or "msl" in p_type_clean:
                feed_shift = float(port_params.get("feed_shift", 0.0))
                meas_plane_shift = float(port_params.get("meas_plane_shift", 0.0))
                msl_length = float(port_params.get("msl_length", 5.0))

                # If shifts are 0, use proportional fractions of the uniform port section
                if feed_shift == 0.0:
                    # The PML is typically ~1.0-1.5mm thick. Force the feed shift to 2.0mm minimum.
                    feed_shift = max(msl_length * 0.2, 2.0)
                if meas_plane_shift == 0.0:
                    # Push the measurement plane 1.0mm past the excitation feed
                    meas_plane_shift = feed_shift + 1.0

                port = FDTD.AddMSLPort(
                    port_nr,
                    metal_prop,
                    p_start,
                    p_stop,
                    prop_dir,
                    exc_dir,
                    excite=excite_val,
                    FeedShift=feed_shift,
                    MeasPlaneShift=meas_plane_shift,
                    priority=10000
                )
                print(f"[*] Registered Port {port_nr} as MSL Port (prop_dir='{prop_dir}', exc_dir='{exc_dir}')")

            else:
                port = FDTD.AddLumpedPort(
                    port_nr,
                    R,
                    p_start,
                    p_stop,
                    exc_dir,
                    excite=excite_val,
                    priority=10000
                )
                print(f"[*] Registered Port {port_nr} natively as LumpedPort (excite={excite_val:+.1f}, R={R:.2f} Ohm, dir='{exc_dir}')")

            print(f"[*] Registered Port {port_nr} as {p_type_clean}")
            return port
        except Exception as e:
            print(f"[!] Failed to create port {port_nr} type='{p_type}': {e}")
            raise

    def setup_ports(self, FDTD, active_port=None):
        manual_ports = self.config.raw_data.get("manual_ports", {})
        self.ports_list = []
        port_nr = 1

        for p_key, p_info in manual_ports.items():
            mode = p_info.get("mode", "Direct Differential")
            port_type = p_info.get("type", "Lumped")
            port_z0 = float(p_info.get("impedance", 50.0))

            pos_term = p_info.get("positive_terminal", {})
            neg_term = p_info.get("negative_terminal", {})

            pos_pad = self._find_pad(pos_term.get("kiid", ""))
            neg_pad = self._find_pad(neg_term.get("kiid", ""))

            if "Single-Ended" in mode:
                if pos_pad is None:
                    raise RuntimeError(f"{p_key}: missing Positive Pad for Single-Ended mode.")
            else:
                if pos_pad is None or neg_pad is None:
                    raise RuntimeError(f"{p_key}: missing pads for Differential mode.")

            sig_layer = p_info.get("signal_layer", "F.Cu")
            ref_layer = p_info.get("reference_layer", "In1.Cu")

            sig_data = self.config.layer_z.get(sig_layer, {})
            ref_data = self.config.layer_z.get(ref_layer, {})

            z_sig = sig_data.get("z", 0.0)
            z_ref = ref_data.get("z", 0.0)

            actual_z0 = port_z0 / 2.0 if "Differential" in mode else port_z0

            def build_pad_port(pad, excitation, term_data):
                px, py = to_csx(pad["x"], pad["y"], self.config.center_x, self.config.center_y)

                pad_shapes = pad.get("pad_shapes", [])
                if pad_shapes:
                    r_x = pad_shapes[0].get("size_x_mm", 1.0) / 2.0
                    r_y = pad_shapes[0].get("size_y_mm", 1.0) / 2.0
                else:
                    r_x, r_y = 0.5, 0.5

                p_type_clean = port_type.lower()

                if "msl" in p_type_clean or "microstrip" in p_type_clean:
                    prop_axis = term_data.get("prop_dir", p_info.get("current_prop_dir", "x"))
                    dir_sign = int(term_data.get("direction_sign", p_info.get("current_direction_sign", 1)))
                    msl_length = float(term_data.get("msl_length", p_info.get("msl_length", 50.0)))
                    feed_shift = float(term_data.get("feed_shift", p_info.get("feed_shift", 4.48)))
                    meas_shift = float(term_data.get("meas_plane_shift", p_info.get("meas_plane_shift", 16.67)))
                    # MSLPort: start[2] must be signal metal (z_sig), stop[2] must be GND (z_ref)
                    if prop_axis == 'x':
                        p_start_x = px - r_x if dir_sign == 1 else px + r_x
                        p_stop_x = p_start_x + (msl_length * dir_sign)
                        p_start = [p_start_x, py - r_y, z_sig]
                        p_stop  = [p_stop_x,  py + r_y, z_ref]
                    else:
                        p_start_y = py - r_y if dir_sign == 1 else py + r_y
                        p_stop_y = p_start_y + (msl_length * dir_sign)
                        p_start = [px - r_x, p_start_y, z_sig]
                        p_stop  = [px + r_x, p_stop_y,  z_ref]

                    p_info["current_prop_dir"] = prop_axis
                    return self._create_openems_port(
                        FDTD, port_nr, p_start, p_stop, "z", port_type, -excitation, R=actual_z0, port_params=p_info
                    )

                else:
                    # LumpedPort: start at GND (z_ref), stop at Signal (z_sig)
                    p_start = [px - r_x, py - r_y, z_ref]
                    p_stop  = [px + r_x, py + r_y, z_sig]
                    return self._create_openems_port(
                        FDTD, port_nr, p_start, p_stop, "z", port_type, excitation, R=actual_z0, port_params=p_info
                    )

            if getattr(self.config, 'extract_matrix', False):
                excite_p = 1.0 if port_nr == active_port else 0.0
                self.ports_list.append(build_pad_port(pos_pad, excite_p, pos_term))
                port_nr += 1

                if mode != "Single-Ended" and neg_pad:
                    excite_n = 1.0 if port_nr == active_port else 0.0
                    self.ports_list.append(build_pad_port(neg_pad, excite_n, neg_term))
                    port_nr += 1

            elif mode in ["Mixed-Mode Differential", "Direct Differential"]:
                excite = 1.0 if port_nr == 1 else 0.0
                self.ports_list.append(build_pad_port(pos_pad, +excite, pos_term))
                port_nr += 1
                self.ports_list.append(build_pad_port(neg_pad, -excite, neg_term))
                port_nr += 1

            else:
                self.ports_list.append(build_pad_port(pos_pad, 1.0 if port_nr == 1 else 0.0, pos_term))
                port_nr += 1

            px, py = float(pos_pad["x"]), float(pos_pad["y"])
            if neg_pad:
                nx, ny = float(neg_pad["x"]), float(neg_pad["y"])
                spacing = np.hypot(nx - px, ny - py)
            else:
                nx, ny = px, py
                spacing = 0.0
            width = float(pos_pad.get("size_mm", 1.0))

            self.port_geometry.append({
                "name": p_key,
                "placement": "pad_exact",
                "center_x": pos_pad["x"],
                "center_y": pos_pad["y"],
                "positive_x": px, "positive_y": py,
                "negative_x": nx, "negative_y": ny,
                "pair_spacing_mm": spacing,
                "trace_width_mm": width,
                "reference_layer": ref_layer,
                "signal_layer": sig_layer
            })

        if self.validation_errors:
            for error in self.validation_errors: print(f"[!] {error}")
            raise RuntimeError("Port geometry validation failed.")

# ==========================================
#  (OpenEMSEngine)
# ==========================================
class OpenEMSEngine:
    def __init__(self, json_path="simulation_metadata.json"):
        print(f"[*] Initializing OpenEMSEngine with: {json_path}")
        self.json_path = json_path  
        self.config = SimulationConfig(json_path)
        self.CSX = CSXGeometryLogger(ContinuousStructure())
        self.FDTD = openEMS.openEMS(
            NrTS=self.config.max_timesteps,
            EndCriteria=10.0 ** (self.config.energy_limit_db / 10.0)
        )
        self.FDTD.SetCSX(self.CSX.GetCSX())

        ex_type = str(self.config.excitation_type).lower()

        if "step" in ex_type.lower():
            self.FDTD.SetStepExcite(self.config.f_max)
        elif "sinus" in ex_type.lower():
            self.FDTD.SetSinusExcite(self.config.f_center)
        elif "dirac" in ex_type.lower():
            self.FDTD.SetDiracExcite(self.config.f_max)
        else:  # Default to Gaussian Pulse
            self.FDTD.SetGaussExcite(self.config.f_center, self.config.f_bandwidth / 2.0)

        pml_str = f"PML_{self.config.pml_cells_count}"

        def parse_bc(bc_val):
            return pml_str if bc_val == "PML" else bc_val

        bcs = self.config.boundary_conditions
        bc_list = [
            parse_bc(bcs.get("x_neg", "PML")),
            parse_bc(bcs.get("x_pos", "PML")),
            parse_bc(bcs.get("y_neg", "PML")),
            parse_bc(bcs.get("y_pos", "PML")),
            parse_bc(bcs.get("z_neg", "PML")),
            parse_bc(bcs.get("z_pos", "PML"))
        ]

        self.FDTD.SetBoundaryCond(bc_list)
        print(f"[*] Configured FDTD Boundaries: {bc_list}")

        self.mesh_mgr = MeshManager(self.config)
        self.geometry = GeometryBuilder(self.CSX, self.config, self.mesh_mgr)
        self.port_mgr = PortManager(self.CSX, self.config, self.geometry)

    def launch_paraview(self):
        print(f"[*] Launching ParaView to visualize 3D Fields from: {self.config.sim_dir}")

        pvd_path = os.path.join(self.config.sim_dir, "E_Field.pvd")
        h5_path = os.path.join(self.config.sim_dir, "E_Field.h5")

        # Prefer the native ParaView animation index (.pvd)
        if os.path.exists(pvd_path):
            target_path = pvd_path
            print("[*] Found VTK/PVD animation file.")

        # Fall back to raw HDF5 (.h5) if file_type=1 was used
        elif os.path.exists(h5_path):
            target_path = h5_path
            print("[*] Found raw HDF5 data file.")
            print("[!] Note: ParaView may ask you to select an HDF5 reader plugin (e.g., XDMF).")

        # Last resort: just open the folder
        else:
            target_path = self.config.sim_dir
            print("[*] No E_Field dump found. Opening parent directory.")

        try:
            if os.name == 'nt':
                paraview_folder = list(filter(lambda x: "ParaView" in x, os.listdir(r"C:\Program Files")))[0]
                paraview_exe = fr"C:\Program Files\{paraview_folder}\bin\paraview.exe"
                subprocess.Popen(f'"{paraview_exe}" "{target_path}"', shell=True, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                subprocess.Popen(["paraview", target_path], creationflags=subprocess.CREATE_NO_WINDOW)

        except Exception as e:
            print(f"[!] Failed to launch ParaView: {e}")
            print("-> Ensure ParaView is installed and added to your system's Environment Variables PATH.")

    def _plot_mixed_mode_s4p(self, freq, s_matrix):
        if not HAS_MATPLOTLIB: return
        s11, s21, s12, s22 = s_matrix[0, 0, :], s_matrix[1, 0, :], s_matrix[0, 1, :], s_matrix[1, 1, :]
        s31, s41, s32, s42 = s_matrix[2, 0, :], s_matrix[3, 0, :], s_matrix[2, 1, :], s_matrix[3, 1, :]
        s_dd11 = 0.5 * (s11 - s21 - s12 + s22)
        s_dd21 = 0.5 * (s31 - s41 - s32 + s42)

        plt.figure(figsize=(12, 7))
        plt.plot(freq / 1e9, 20.0 * np.log10(np.abs(s_dd11) + 1e-12), label='Sdd11 (Return Loss)', color='red')
        plt.plot(freq / 1e9, 20.0 * np.log10(np.abs(s_dd21) + 1e-12), label='Sdd21 (Insertion Loss)', color='blue')
        plt.title('Mixed-Mode Differential S-Parameters')
        plt.xlabel('Frequency (GHz)')
        plt.ylabel('Magnitude (dB)')
        plt.grid(True, linestyle='--')
        plt.legend()
        plt.savefig(os.path.join(self.config.sim_dir, "s_parameter_plot.png"))

    def post_process_sNp_matrix(self, run_results, total_ports):
        freq = np.linspace(self.config.f_start, self.config.f_stop, self.config.f_num_points)
        s_matrix = np.zeros((total_ports, total_ports, len(freq)), dtype=complex)

        for active_port, port_dir in run_results.items():
            print(f"[*] Processing data for Port {active_port}...")
            dummy_engine = OpenEMSEngine(self.json_path)
            dummy_engine.config.sim_dir = port_dir
            dummy_engine.build_model(active_port=active_port)

            for port in dummy_engine.port_mgr.ports_list:
                port.CalcPort(port_dir, freq, ref_impedance=self.config.port_reference_impedance)

            col = active_port - 1
            inc_wave = dummy_engine.port_mgr.ports_list[col].uf_inc
            denominator = np.where(np.abs(inc_wave) > 1e-15, inc_wave, 1e-15 + 0j)

            for r, port_obj in enumerate(dummy_engine.port_mgr.ports_list):
                numerator = port_obj.uf_ref if r == col else port_obj.uf_tot
                s_matrix[r, col, :] = numerator / denominator

        # Reciprocity mirroring for unsimulated runs
        if self.config.assume_symmetry and len(run_results) < total_ports:
            print(f"[*] Applying reciprocal symmetry mirror (S_ij = S_ji)...")
            for i in range(total_ports):
                for j in range(total_ports):
                    if (j + 1) not in run_results:
                        s_matrix[i, j, :] = s_matrix[j, i, :]

        # Export via scikit-rf (handles dynamic .s1p, .s2p, .s4p, .sNp extension)
        snp_path = os.path.join(self.config.sim_dir, f"simulation_results_full.s{total_ports}p")
        net = export_network_to_touchstone(snp_path, freq, s_matrix, z0=self.config.port_reference_impedance)

        # Plot Mixed-Mode directly via scikit-rf if 4 ports
        if total_ports == 4 and HAS_MATPLOTLIB:
            plot_touchstone(snp_path)

    def post_process_s_parameters(self):
        if not self.port_mgr.ports_list:
            return

        print("[*] Post-processing: Calculating S-Parameters...")
        freq_array = np.linspace(self.config.f_start, self.config.f_stop, getattr(self.config, 'f_num_points', 1001))
        f_ghz = freq_array / 1e9
        num_f = len(freq_array)
        num_ports = len(self.port_mgr.ports_list)

        for p in self.port_mgr.ports_list:
            p.CalcPort(self.config.sim_dir, freq_array, ref_impedance=self.config.port_reference_impedance)

        if 'HAS_MATPLOTLIB' in globals() and HAS_MATPLOTLIB:
            plt.figure(figsize=(12, 7))

        # --- 2-Port Matrix (Direct Differential / Single-Ended paired) ---
        if num_ports == 2:
            s11 = self.port_mgr.ports_list[0].uf_ref / self.port_mgr.ports_list[0].uf_inc
            s21 = self.port_mgr.ports_list[1].uf_tot / self.port_mgr.ports_list[0].uf_inc

            if 'HAS_MATPLOTLIB' in globals() and HAS_MATPLOTLIB:
                plt.plot(f_ghz, 20.0 * np.log10(np.abs(s11) + 1e-12), label='S11 (Return Loss)', color='red',
                         linewidth=2)
                plt.plot(f_ghz, 20.0 * np.log10(np.abs(s21) + 1e-12), label='S21 (Insertion Loss)', color='blue',
                         linewidth=2)

            ts_path = os.path.join(self.config.sim_dir, "simulation_results.s2p")
            write_touchstone_s2p(ts_path, freq_array, s11, s21, z0=self.config.port_reference_impedance)

        # --- 1-Port Matrix (Isolated Single-Ended) ---
        elif self.config.total_physical_ports == 1:
            s11 = self.port_mgr.ports_list[0].uf_ref / self.port_mgr.ports_list[0].uf_inc

            if 'HAS_MATPLOTLIB' in globals() and HAS_MATPLOTLIB:
                plt.plot(f_ghz, 20.0 * np.log10(np.abs(s11) + 1e-12), label='S11 (Return Loss)', color='red',
                         linewidth=2)

            ts_path = os.path.join(self.config.sim_dir, "simulation_results.s1p")
            write_touchstone_s1p(ts_path, freq_array, s11, z0=self.config.port_reference_impedance)

        if 'HAS_MATPLOTLIB' in globals() and HAS_MATPLOTLIB:
            plt.title('S-Parameters Plot (openEMS)')
            plt.xlabel('Frequency (GHz)')
            plt.ylabel('Magnitude (dB)')
            plt.grid(True, which='both', linestyle='--', linewidth=0.5)
            plt.legend(loc='best')
            plt.savefig(os.path.join(self.config.sim_dir, "s_parameter_plot.png"))
            plt.pause(10)

    def calculate_mesh_summary(self):
        print("[*] Generating geometry and building Mesh for diagnostics...")
        self.build_model()

        # Retrieve the generated grid directly from the CSX engine
        grid = self.CSX.GetGrid()

        # Safely evaluate the NumPy arrays by checking length ---
        gx = grid.GetLines('x')
        gy = grid.GetLines('y')
        gz = grid.GetLines('z')

        x_lines = np.sort(np.array(gx)) if gx is not None and len(gx) > 0 else np.array([])
        y_lines = np.sort(np.array(gy)) if gy is not None and len(gy) > 0 else np.array([])
        z_lines = np.sort(np.array(gz)) if gz is not None and len(gz) > 0 else np.array([])

        nx = max(0, len(x_lines) - 1)
        ny = max(0, len(y_lines) - 1)
        nz = max(0, len(z_lines) - 1)
        total_cells = nx * ny * nz

        print("\n" + "=" * 50)
        print(" MESH DIAGNOSTICS & VALIDATION")
        print("=" * 50)
        print(f"[*] Total FDTD Cells: {total_cells:,} ({nx} x {ny} x {nz})")

        warnings = 0

        # --- CHECK 1: Total Cell Count ---
        if total_cells > 15_000_000:
            print("[!] WARNING: Cell count exceeds 15 million. This will require massive RAM and long runtimes.")
            warnings += 1

        # --- CHECK 2 & 3: Courant Stability and Nanometer Slivers ---
        min_dx = np.min(np.diff(x_lines)) if len(x_lines) > 1 else float('inf')
        min_dy = np.min(np.diff(y_lines)) if len(y_lines) > 1 else float('inf')
        min_dz = np.min(np.diff(z_lines)) if len(z_lines) > 1 else float('inf')

        absolute_min_cell = min(min_dx, min_dy, min_dz)
        print(f"[*] Absolute Smallest Cell: {absolute_min_cell:.6f} mm")

        # Check for catastrophic nanometer/sub-micron slivers
        if absolute_min_cell < 0.001:
            print(f"[!] CRITICAL WARNING: Microscopic cell detected ({absolute_min_cell:.6f} mm).")
            print("    -> This breaks the Courant limit. Simulation timestep will plunge and likely crash.")
            warnings += 1

        # Check for algorithm integrity (The 1/3-Gap Rule constraint)
        # We multiply by 0.98 to account for tiny floating point rounding offsets
        expected_min = (self.config.min_cell_size_mm / 3.0) * 0.98
        if absolute_min_cell < expected_min and absolute_min_cell >= 0.001:
            print(f"[!] WARNING: Cell size is unexpectedly small.")
            print(f"    -> Expected minimum is ~{expected_min:.4f} mm based on your 1/3 gap rule.")
            warnings += 1

        print("-" * 50)
        if warnings == 0:
            print("[SUCCESS] Mesh validation PASSED. Grid is stable and highly optimized.\n")
        else:
            print(f"[FAILED] Mesh validation caught {warnings} issue(s). Review your parameters.\n")

        return total_cells

    def build_model(self, active_port=None):
        print("[*] Building Stackup & Materials...")
        self.geometry.build_materials_and_dielectrics()

        print("[*] Drawing Copper Features & Vias...")
        self.geometry.draw_copper_features()

        print("[*] Drawing RLC Components...")
        self.geometry.draw_rlc_components()

        # Step 1: Pre-register port coordinates to protect mesh boundaries
        self.port_mgr.register_port_mesh_keys()

        # Step 2: Finalize the Smart Mesh so mesh.GetLines() is populated
        print("[*] Finalizing Smart Mesh...")
        self.mesh_mgr.finalize_mesh(
            self.CSX,
            self.geometry.key_x_points,
            self.geometry.key_y_points,
            self.geometry.key_z_points
        )

        # Step 3: Instantiate ports now that grid lines exist
        print(f"[*] Setting up Ports (Active Port: {active_port if active_port else 'Differential'})...")
        self.port_mgr.setup_ports(self.FDTD, active_port=active_port)

        print("[*] Port geometry summary:")
        for p in self.port_mgr.port_geometry:
            print(f"    {p['name']}: placement={p['placement']}, P/N Spacing={p['pair_spacing_mm']:.3f} mm, Width={p['trace_width_mm']:.3f} mm")

        grid = self.CSX.GetGrid()
        gx = grid.GetLines('x')
        gy = grid.GetLines('y')
        gz = grid.GetLines('z')

        if gx is not None and len(gx) > 0 and gy is not None and len(gy) > 0 and gz is not None and len(gz) > 0 and hasattr(self.geometry, 'air_box_mat'):
            self.geometry.air_box_mat.AddBox(
                [min(gx), min(gy), min(gz)],
                [max(gx), max(gy), max(gz)],
                priority=9600
            )
            print("[*] Air Box explicitly bounded to optimal mesh limits.")

    def preview_geometry(self, sim_dir=None):
        if sim_dir is None:
            base_temp = os.path.expandvars("%TEMP%")
            sim_dir = os.path.join(base_temp, "openEMS_Sim_Dir")
        self.build_model()
        os.makedirs(sim_dir, exist_ok=True)
        xml_path = os.path.join(sim_dir, "geometry_preview.xml")
        self.CSX.Write2XML(xml_path)
        print("[*] Launching AppCSXCAD 3D Geometry Viewer...")
        try:
            subprocess.Popen([AppCSXCAD_BIN, xml_path], creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception as e:
            print(f"[!] Failed to launch AppCSXCAD: {e}")

    def run_simulation(self):
        if not self.config.extract_matrix:
            print("\n[*] Commencing Direct Differential FDTD Run...")
            sim_dir = self.config.sim_dir
            if not os.path.exists(sim_dir):
                os.makedirs(sim_dir)

            self.build_model(active_port=None)
            print("[*] Setting up 3D Time-Domain Field Dumps...")
            z_min = min(self.geometry.key_z_points)
            z_max = max(self.geometry.key_z_points)

            # Define the bounding box covering your entire board area
            start_box = [self.config.board_min_x, self.config.board_min_y, z_min]
            stop_box = [self.config.board_max_x, self.config.board_max_y, z_max]

            # E-Field Time-Domain Dump (dump_type=0, file_type=1 for HDF5/VTK export)
            dump_e = self.CSX.AddDump('E_Field', dump_type=0, file_type=0)
            dump_e.AddBox(start_box, stop_box)

            # H-Field Time-Domain Dump (dump_type=1, file_type=1 for HDF5/VTK export)
            dump_h = self.CSX.AddDump('H_Field', dump_type=1, file_type=0)
            dump_h.AddBox(start_box, stop_box)

            # Structure/Geometry Dump (dump_type=10 for Material Index)
            dump_struct = self.CSX.AddDump('Structure', dump_type=10, file_type=0)
            dump_struct.AddBox(start_box, stop_box)

            print("[*] Launching FDTD Solver...")
            try:
                remove_abort_file(sim_dir)
                self.FDTD.Run(sim_dir, cleanup=True)
                remove_abort_file(sim_dir)
                print("\n--- Simulation Complete ---")
                self.post_process_s_parameters()
                convert_s_params_to_spice(os.path.join(self.config.sim_dir, "simulation_results.s2p"))
                sys.exit(0)
            except Exception as e:
                print(f"\n[!] CRITICAL: Simulation failed. {e}")
                sys.exit(1)
        else:
            print(f"\n[*] Commencing Rigorous Single-Ended {self.config.total_physical_ports}-Port Sweep...")
            # Automatically map runs for N physical ports
            ports_to_run = list(
                range(1, self.config.total_physical_ports + 1, 2)) if self.config.assume_symmetry else list(
                range(1, self.config.total_physical_ports + 1))
            run_results = {}

            if self.config.parallel_sim:
                threads = self.config.num_threads if self.config.num_threads > 0 else len(ports_to_run)
                print(f"[*] Launching Parallel FDTD Pool with {threads} threads...")
                with multiprocessing.Pool(threads) as pool:
                    results = pool.starmap(run_single_port_worker,
                                               [(self.json_path, p, self.config.total_physical_ports) for p in
                                                ports_to_run])
                for p_num, p_dir in results:
                    run_results[p_num] = p_dir
            else:
                for p in ports_to_run:
                    p_num, p_dir = run_single_port_worker(self.json_path, p, self.config.total_physical_ports)
                    run_results[p_num] = p_dir
            print("\n[*] All FDTD runs complete. Stitching data matrix...")
            self.post_process_sNp_matrix(run_results, self.config.total_physical_ports)
            convert_s_params_to_spice(
                os.path.join(self.config.sim_dir, f"simulation_results_full.s{self.config.total_physical_ports}p"))
            sys.exit(0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KiCad Electrodynamic & Static Studio - OפקמEMS FDTD Engine")
    parser.add_argument("json_file", nargs="?", default="simulation_metadata.json", help="Path to JSON metadata")
    parser.add_argument("--view", action="store_true", help="Preview 3D geometry in AppCSXCAD")
    parser.add_argument("--run", action="store_true", help="Run full FDTD simulation solver")
    parser.add_argument("--calc-mesh", action="store_true", help="Calculate mesh cell count for GUI")
    parser.add_argument("--plot", type=str, metavar="FILE", help="Path to a Touchstone (.s2p or .s4p) file to plot")
    parser.add_argument("--paraview", action="store_true", help="Launch ParaView to animate 3D fields")

    args = parser.parse_args()

    # Intercept the plot flag before initializing the heavy engine
    if args.plot:
        plot_touchstone(args.plot)
        sys.exit(0)

    # If the user clicks "Run" in Windows, intercept it and hand the entire Python script to Linux
    # --- THE WSL2 BRIDGE ---
    if os.name == 'nt' and args.run:
        use_wsl2 = False
        if os.path.exists(args.json_file):
            try:
                with open(args.json_file, 'r', encoding='utf-8') as f:
                    use_wsl2 = json.load(f).get("use_wsl2", False)
            except Exception:
                pass

        if use_wsl2:
            print("[*] WSL2 toggle active. Delegating execution to Linux Engine...")
            script_path = os.path.abspath(__file__).replace('\\', '/')
            if script_path[1] == ':':
                script_path = '/mnt/' + script_path[0].lower() + script_path[2:]

            json_arg = os.path.abspath(args.json_file).replace('\\', '/')
            if json_arg[1] == ':':
                json_arg = '/mnt/' + json_arg[0].lower() + json_arg[2:]

            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = subprocess.CREATE_NO_WINDOW
            ret = subprocess.call(["wsl", "python3", script_path, json_arg, "--run"],
                                  stdout=sys.stdout,
                                  stderr=subprocess.STDOUT,
                                  startupinfo=startupinfo,
                                  creationflags=creationflags)
            sys.exit(ret)
        else:
            print("[*] Native Windows execution selected. Bypassing WSL2...")

    # --- NATIVE EXECUTION ---
    # If we get here with args.run, we are already successfully inside Linux!
    engine = OpenEMSEngine(args.json_file)

    if args.calc_mesh:
        engine.calculate_mesh_summary()
    elif args.view:
        engine.preview_geometry()
    elif args.paraview:
        engine.launch_paraview()
    else:
        if os.name == 'nt':
            os.system("taskkill /f /im openEMS.exe >nul 2>&1")
        else:
            os.system("pkill -x openEMS >/dev/null 2>&1")
        engine.run_simulation()
