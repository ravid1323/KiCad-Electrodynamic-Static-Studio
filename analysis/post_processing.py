import os, sys
import numpy as np
import json
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon
import pandas as pd
import scipy.signal
import skrf as rf
from skrf.vectorFitting import VectorFitting
import sympy as sp


def convert_s_params_to_spice(touchstone_path, dc_ir_file=None, output_cir_file=None, num_poles=5, z0=50.0):
    """
    Converts a Touchstone S-parameter file into a SPICE subcircuit (.cir).
    Optionally merges DC IR drop data to synthesize a broadband model starting at 0 Hz.
    """
    if not os.path.exists(touchstone_path):
        print(f"[!] Error: File not found -> {touchstone_path}")
        return None

    print(f"[*] Loading network from {touchstone_path}...")
    net = rf.Network(touchstone_path, z0=z0)
    num_ports = net.number_of_ports
    print(f"[*] Detected {num_ports}-Port Network.")

    n_poles_real = 1
    if dc_ir_file and os.path.exists(dc_ir_file):
        print(f"[*] Integrating DC IR data from {dc_ir_file}...")
        with open(dc_ir_file, 'r') as f:
            dc_data = json.load(f)

        if 'networks' in dc_data and len(dc_data['networks']) > 0:
            first_net = list(dc_data['networks'].values())[0]
            r_dc = first_net['load_actual']['total_loop_r_ohms']
        else:
            r_dc = 1e-6  # Fallback if empty

        s11_dc = r_dc / (r_dc + 2 * z0)
        s21_dc = (2 * z0) / (r_dc + 2 * z0)
        s_matrix_dc = np.array([[s11_dc, s21_dc],
                                [s21_dc, s11_dc]])

        if net.f[0] <= 1e-6:
            net.s[0] = s_matrix_dc
        else:
            freq_dc = rf.Frequency(0, 0, 1, unit='hz')
            ntwk_dc = rf.Network(frequency=freq_dc, s=np.array([s_matrix_dc]), z0=z0)
            net = rf.stitch(ntwk_dc, net)

        n_poles_real = 4

    vf = VectorFitting(net)
    print(f"[*] Performing Vector Fitting with {n_poles_real} real poles and {num_poles} complex pole pairs...")
    vf.vector_fit(n_poles_real=n_poles_real, n_poles_cmplx=num_poles)

    if not vf.is_passive():
        print("[*] Model is non-passive. Enforcing passivity for SPICE stability...")
        vf.passivity_enforce()
    else:
        print("[*] Model is inherently passive. No enforcement needed.")

    if not output_cir_file:
        base_name = os.path.splitext(os.path.basename(touchstone_path))[0]
        output_dir = os.path.dirname(touchstone_path)
        output_cir_file = os.path.join(output_dir, f"{base_name}_model.cir")
    else:
        base_name = os.path.splitext(os.path.basename(output_cir_file))[0]

    vf.write_spice_subcircuit_s(output_cir_file)
    print(f"[*] SPICE Subcircuit successfully exported -> {output_cir_file}")

    pins = " ".join([f"p{i + 1}" for i in range(num_ports)])
    print(f"[*] SPICE Definition: .subckt {base_name} {pins} ref")

    return vf

def convert_s_params_to_spice_lib(touchstone_path, num_poles=5):
    """
    Converts a Touchstone S-parameter file into a SPICE library (.lib)
    using Vector Fitting, dynamically assigning pins based on port count.
    """
    if not os.path.exists(touchstone_path):
        print(f"[!] Error: File not found -> {touchstone_path}")
        return

    print(f"[*] Loading network from {touchstone_path}...")
    net = rf.Network(touchstone_path)
    num_ports = net.number_of_ports
    print(f"[*] Detected {num_ports}-Port Network.")

    vf = VectorFitting(net)

    print(f"[*] Performing Vector Fitting with {num_poles} complex pole pairs...")
    vf.vector_fit(n_poles_real=1, n_poles_cmplx=num_poles)

    if not vf.is_passive():
        print("[*] Model is non-passive. Enforcing passivity for SPICE stability...")
        vf.passivity_enforce()
    else:
        print("[*] Model is inherently passive. No enforcement needed.")

    base_name = os.path.splitext(os.path.basename(touchstone_path))[0]
    output_dir = os.path.dirname(touchstone_path)
    lib_path = os.path.join(output_dir, f"{base_name}_model.lib")

    vf.write_spice_subcircuit_s(lib_path)

    print(f"[*] SPICE Library successfully exported -> {lib_path}")

    pins = " ".join([f"p{i + 1}" for i in range(num_ports)])
    print(f"[*] SPICE Pinout Mapping: .subckt {base_name}_model {pins} ref")



def create_kicad_laplace_model(tf_string: str, subckt_name: str = "LAPLACE_TF", path: str = None) -> str:
    """
    Generates a KiCad (ngspice) compatible SPICE subcircuit for a Laplace transfer function,
    and optionally saves it to a file.

    Args:
        tf_string (str): The transfer function string (e.g., "100 / (s**2 + 10*s + 100)").
                         Use standard Python math syntax (e.g., ** for exponents).
        subckt_name (str): The name of the generated subcircuit.
        path (str, optional): The file path to save the SPICE model (e.g., "models/filter.lib").
                                   If None, the model is only returned as a string.

    Returns:
        str: The formatted ngspice subcircuit definition.
    """
    safe_tf_string = tf_string.replace('^', '**')

    s = sp.Symbol('s')

    try:
        expr = sp.simplify(sp.sympify(safe_tf_string))

        num, den = sp.fraction(expr)

        num_poly = sp.Poly(num, s)
        den_poly = sp.Poly(den, s)

        num_coeffs = [float(c) for c in num_poly.all_coeffs()]
        den_coeffs = [float(c) for c in den_poly.all_coeffs()]

    except Exception as e:
        raise ValueError(f"Failed to parse transfer function: {e}\nWe allow '^' for exponents")

    num_str = " ".join(f"{c:g}" for c in num_coeffs)
    den_str = " ".join(f"{c:g}" for c in den_coeffs)

    spice_model = (
        f"* KiCad (ngspice) Laplace Transfer Function Model\n"
        f"* Original H(s) = {tf_string}\n"
        f".SUBCKT {subckt_name} IN+ IN- OUT+ OUT-\n"
        f"A1 %vd(IN+ IN-) %vd(OUT+ OUT-) {subckt_name}_xfer\n"
        f".MODEL {subckt_name}_xfer s_xfer(num_coeff=[{num_str}] den_coeff=[{den_str}])\n"
        f"R_dummy OUT+ OUT- 1G ; Prevents floating node errors in ngspice\n"
        f".ENDS {subckt_name}\n"
    )

    os.makedirs(os.path.dirname(path), exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        f.write(spice_model)
    print(f"Model successfully saved to: {os.path.abspath(path)}")

    return spice_model


def export_crosstalk(s_param_path):
    """Extracts and plots coupling (crosstalk) parameters from an N-port .sNp file relative to Port 1."""
    net = rf.Network(s_param_path)
    freq_ghz = net.f / 1e9
    num_ports = net.number_of_ports

    if num_ports < 2:
        print("[!] Error: Crosstalk requires at least a 2-port network.")
        return

    plt.figure(figsize=(10, 6))

    colors = plt.cm.plasma(np.linspace(0, 0.8, num_ports - 1))

    for i in range(1, num_ports):
        coupling_db = 20 * np.log10(np.abs(net.s[:, i, 0]) + 1e-12)
        plt.plot(freq_ghz, coupling_db, label=f'Coupling ($S_{{{i + 1}1}}$)', color=colors[i - 1], linewidth=2)

    plt.title(f'{num_ports}-Port Coupling Overview')
    plt.xlabel('Frequency (GHz)')
    plt.ylabel('Magnitude (dB)')
    plt.grid(True, linestyle='--')
    plt.legend()
    plt.ylim([-100, 0])

    out_png = s_param_path.replace(os.path.splitext(s_param_path)[1], "_plot_crosstalk.png")
    plt.savefig(out_png)
    print(f"[*] Crosstalk Plot successfully saved to {out_png}")
    plt.close()


def export_tdr_impedance(s_param_path, target_z=50.0, tolerance=0.1, epsilon_r=4.2):
    """Calculates TDR using native skrf step_response logic for an N-Port network."""
    net_raw = rf.Network(s_param_path)

    # Extrapolate to DC for accurate time-domain transformation
    net = net_raw.extrapolate_to_dc(kind='linear')
    num_ports = net.number_of_ports

    if num_ports >= 4:
        # Assuming standard [P1(+), P2(-)] -> [P3(+), P4(-)] layout
        s11, s12 = net.s[:, 0, 0], net.s[:, 0, 1]
        s21, s22 = net.s[:, 1, 0], net.s[:, 1, 1]
        s_target_array = 0.5 * (s11 - s12 - s21 + s22)
        ref_z = 100.0  # Differential reference
        mode_label = 'Differential'
    else:
        s_target_array = net.s[:, 0, 0]
        ref_z = 50.0  
        mode_label = 'Single-Ended'

    s_target_array_3d = s_target_array.reshape(-1, 1, 1)
    s_target_net = rf.Network(frequency=net.frequency, s=s_target_array_3d, z0=ref_z)

    # Extract step response with padding for high spatial resolution
    t, rho_step = s_target_net.step_response(window='hamming', pad=5000)
    rho_1d = np.squeeze(rho_step).real

    # Calculate Physical Impedance (adding tiny epsilon to prevent divide-by-zero)
    z_tdr = ref_z * (1 + rho_1d) / (1 - rho_1d + 1e-12)

    # Convert Time to Physical Distance
    c0 = 299792458.0
    velocity_factor = 1 / np.sqrt(epsilon_r)
    distance_mm = (t * c0 * velocity_factor / 2.0) * 1000.0

    plt.figure(figsize=(10, 6))
    plt.plot(distance_mm, z_tdr, label=f'{mode_label} Impedance ($Z_{{TDR}}$)', color='blue', linewidth=2)
    plt.title(f'Time Domain Reflectometry (TDR) - {num_ports}-Port')
    plt.xlabel('Distance from Port (mm)')
    plt.ylabel('Impedance ($\Omega$)')

    plt.axhline(target_z, color='green', linestyle='--', label=f'Target ({target_z} $\Omega$)')
    plt.axhline(target_z * (1 + tolerance), color='red', linestyle='--', label=f'+{tolerance * 100}% Bound')
    plt.axhline(target_z * (1 - tolerance), color='red', linestyle='--', label=f'-{tolerance * 100}% Bound')

    plt.xlim([0, max(50.0, distance_mm[-1] * 0.5)])
    plt.grid(True, linestyle='--')
    plt.legend()

    out_png = s_param_path.replace(os.path.splitext(s_param_path)[1], "_plot_tdr.png")
    plt.savefig(out_png)
    print(f"[*] TDR Plot successfully saved to {out_png}")
    plt.close()

def cluster_and_snap_coordinates(raw_coords, min_cell_size):
    """
    Sorts a 1D array of geometry coordinates and clusters any points that are
    closer than the allowed minimum FDTD cell size to prevent Courant limit crashes.
    """
    if len(raw_coords) == 0:
        return np.array([])

    # Step 1: Remove exact duplicates and sort from smallest to largest
    sorted_coords = np.sort(np.unique(raw_coords))

    final_coords = []
    current_cluster = [sorted_coords[0]]

    # Step 2: Group points that are dangerously close together
    for pt in sorted_coords[1:]:
        # If the distance from the last point in the cluster is less than our safety limit
        if (pt - current_cluster[-1]) < min_cell_size:
            current_cluster.append(pt)
        else:
            # Resolve the cluster by taking the mean (center of gravity)
            final_coords.append(np.mean(current_cluster))
            # Start a new cluster for the next point
            current_cluster = [pt]

    # Append the very last cluster
    final_coords.append(np.mean(current_cluster))

    # Step 3: Strict Safety Verification Pass
    # Ensures no two resolved clusters ended up too close after the averaging process
    safe_coords = [final_coords[0]]
    for pt in final_coords[1:]:
        if (pt - safe_coords[-1]) >= min_cell_size:
            safe_coords.append(pt)
        else:
            # If still too close, forcefully merge them with the previous safe point
            safe_coords[-1] = (safe_coords[-1] + pt) / 2.0

    # Finally, round to 4 decimal places to clean up floating point trailing noise
    return np.round(safe_coords, 4)

def load_time_domain():
    # Load the extension-less file directly
    data = np.loadtxt(r'C:\Users\ravid\Documents\KiCad\10.0\scripting\plugins\em_sim_studio\em_simulation_results\fdtd_results\port_ut_1', comments='%')

    time = data[:, 0]
    amplitude = data[:, 1]

    plt.plot(time, amplitude)
    plt.title("Time Domain Excitation at Port 1")
    plt.show()


def plot_impedance_magnitude(filepath, z0=50.0):
    """Calculates and plots the Impedance Magnitude ||Z_in|| from a Touchstone file."""
    if not os.path.exists(filepath):
        print(f"[!] File not found: {filepath}")
        return

    print(f"[*] Calculating Z(f) for: {filepath}")

    try:
        # 1. Parse Touchstone file, ignoring comments
        with open(filepath, 'r') as f:
            lines = [line.strip() for line in f if
                     not line.startswith('!') and not line.startswith('#') and line.strip()]

        data_vals = []
        for line in lines:
            data_vals.extend([float(x) for x in line.split()])

        # 2. Extract S11 based on format (.s1p or .s2p)
        if filepath.lower().endswith('.s1p'):
            data = np.array(data_vals).reshape(-1, 3)
            freqs_ghz = data[:, 0]
            s11 = data[:, 1] + 1j * data[:, 2]
        elif filepath.lower().endswith('.s2p'):
            # s2p format: Freq Re11 Im11 Re21 Im21 Re12 Im12 Re22 Im22[cite: 11]
            data = np.array(data_vals).reshape(-1, 9)
            freqs_ghz = data[:, 0]
            s11 = data[:, 1] + 1j * data[:, 2]
        else:
            print("[!] Unsupported file extension. Use .s1p or .s2p")
            return

        # 3. Handle physical singularity (prevent division by zero if S11 is perfectly 1.0)
        s11 = np.where(s11 == 1.0 + 0j, 1.0 - 1e-12 + 0j, s11)

        # 4. Calculate Impedance Magnitude
        z_in = z0 * (1 + s11) / (1 - s11)
        z_mag = np.abs(z_in)

        # 5. Generate Plot
        plt.figure(figsize=(10, 6))
        plt.plot(freqs_ghz, z_mag, color='black', linewidth=2, label='$||Z_{in}||$')
        plt.axhline(z0, color='blue', linestyle=':', label=f'Target ({z0} $\Omega$)')

        plt.title('Input Impedance $Z(f)$ vs Frequency')
        plt.xlabel('Frequency (GHz)')
        plt.ylabel('Impedance ($\Omega$)')
        plt.grid(True, linestyle='--')
        plt.legend(loc='best')
        plt.tight_layout()

        # Save and show
        out_png = filepath.replace(os.path.splitext(filepath)[1], "_impedance.png")
        plt.savefig(out_png)
        print(f"[*] Plot successfully saved to {out_png}")

        plt.show()

    except Exception as e:
        print(f"[!] Failed to calculate impedance: {e}")


def plot_eye_diagram_with_mask(csv_path, bitrate_bps=5e9, signal_column_index=1, mask_width_ratio=0.40,
                               mask_height_ratio=0.40, out_path=None):
    """
    Reads a KiCad SPICE CSV export, plots a folded Eye Diagram,
    and adds a semi-transparent diamond mask with dynamic target parameters.
    """
    print(f"[*] Loading data from {csv_path}...")

    # Auto-detect the delimiter
    df = pd.read_csv(csv_path, sep=None, engine='python')
    if df.shape[1] < 2:
        print("[!] Error: Only 1 column detected. Check CSV format.")
        return

    # Extract Time and Voltage
    time_sec = df.iloc[:, 0].values
    voltage = df.iloc[:, signal_column_index].values

    # Clean up simulator resets
    dt = np.diff(time_sec)
    reset_indices = np.where(dt < 0)[0]
    if len(reset_indices) > 0:
        start_idx = reset_indices[-1] + 1
        time_sec = time_sec[start_idx:]
        voltage = voltage[start_idx:]

    # Calculate Unit Intervals
    ui_sec = 1.0 / bitrate_bps
    ui_ps = ui_sec * 1e12
    window_sec = 2.0 * ui_sec
    window_ps = window_sec * 1e12

    time_folded = time_sec % window_sec
    time_folded_ps = time_folded * 1e12
    wrap_indices = np.where(np.diff(time_folded_ps) < 0)[0]

    fig, ax = plt.subplots(figsize=(10, 6))

    # Plot the eye traces
    start_idx = 0
    for end_idx in wrap_indices:
        ax.plot(time_folded_ps[start_idx:end_idx],
                voltage[start_idx:end_idx],
                color='blue', alpha=0.05)
        start_idx = end_idx + 1
    ax.plot(time_folded_ps[start_idx:], voltage[start_idx:], color='blue', alpha=0.05)

    # --- ADDING THE DIAMOND MASK ---
    v_mean = np.mean(voltage)
    v_max = np.max(voltage)
    v_min = np.min(voltage)
    v_amp = v_max - v_min

    # Set mask dimensions
    mask_width_ps = ui_ps * mask_width_ratio
    mask_height_v = v_amp * mask_height_ratio

    # Helper function to generate diamond vertices
    def create_diamond(center_x, center_y, width, height):
        return [
            (center_x - width / 2.0, center_y),  # Left
            (center_x, center_y - height / 2.0),  # Bottom
            (center_x + width / 2.0, center_y),  # Right
            (center_x, center_y + height / 2.0)  # Top
        ]

    # Create and add Mask 1 (Centered in the First UI at 0.5 * UI)
    diamond1_pts = create_diamond(ui_ps * 0.5, v_mean, mask_width_ps, mask_height_v)
    mask1 = Polygon(diamond1_pts, closed=True, linewidth=1, edgecolor='red', facecolor='red', alpha=0.3)
    ax.add_patch(mask1)

    # Create and add Mask 2 (Centered in the Second UI at 1.5 * UI)
    diamond2_pts = create_diamond(ui_ps * 1.5, v_mean, mask_width_ps, mask_height_v)
    mask2 = Polygon(diamond2_pts, closed=True, linewidth=1, edgecolor='red', facecolor='red', alpha=0.3)
    ax.add_patch(mask2)

    # Formatting
    ax.set_title(f'Eye Diagram ({bitrate_bps / 1e9} Gbps) with Compliance Mask')
    ax.set_xlabel('Time (ps)')
    ax.set_ylabel('Voltage (V)')
    ax.grid(True, linestyle='--')
    ax.set_xlim(0, window_ps)
    plt.tight_layout()

    if out_path:
        plt.savefig(out_path)
        print(f"[*] Eye diagram saved to {out_path}")
    else:
        plt.show()
    plt.close()

def calc_tdr_from_s_parameters(touchstone_path):
    net = rf.Network(touchstone_path)
    s11 = net.s[:, 0, 0]
    s12 = net.s[:, 0, 1]
    s21 = net.s[:, 1, 0]
    s22 = net.s[:, 1, 1]
    s33 = net.s[:, 2, 2]
    s34 = net.s[:, 2, 3]
    s43 = net.s[:, 3, 2]
    s44 = net.s[:, 3, 3]
    z11 = net.z[:, 0, 0]
    z12 = net.z[:, 0, 1]
    z21 = net.z[:, 1, 0]
    z22 = net.z[:, 1, 1]
    sdd11 = 0.5 * (s11 + s22 - s21 - s12)
    scc11 = 0.5 * (s11 + s22 + s21 + s12)
    sdd22 = 0.5 * (s33 + s44 - s34 - s43)
    scc22 = 0.5 * (s33 + s44 + s34 + s43)
    z_oc = z11
    z_sc = z11 - (z12*z21)/(z22)
    z_line = np.sqrt(z_sc*z_oc)
    z0 = np.average(net.z0)
    rho_net = rf.Network(frequency=net.frequency, s=s11, z0=z0)
    t, rho_step = rho_net.step_response(window='hamming', pad=5000)
    z_t = z0*(1+rho_step)/(1-rho_step)
    plt.plot(t, z_t)
    plt.show()
    pass
