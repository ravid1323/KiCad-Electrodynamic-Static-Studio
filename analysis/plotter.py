import os
import json
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as patches
import matplotlib.colors as mcolors
import matplotlib.cm as cm
import matplotlib.ticker as ticker
import wx
import io

def _fig_to_bitmap(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=100)
    plt.close(fig)
    buf.seek(0)
    image = wx.Image(buf, wx.BITMAP_TYPE_PNG)
    return wx.Bitmap(image)

def _get_eff_length(lines):
    N = len(lines)
    eff = np.zeros(N)
    if N == 1:
        return np.array([1.0])
    eff[0] = (lines[1] - lines[0]) / 2.0
    eff[-1] = (lines[-1] - lines[-2]) / 2.0
    if N > 2:
        eff[1:-1] = (lines[2:] - lines[:-2]) / 2.0
    return eff

def plot_potential_map(potential_3d, x_coords, y_coords, z_coords, sigma_map, target_z, filename):
    Nx, Ny, Nz = len(x_coords), len(y_coords), len(z_coords)
    sigma_3d = sigma_map.reshape((Nx, Ny, Nz), order="C")
    z_idx = np.argmin(np.abs(z_coords - target_z))

    V_slice = potential_3d[:, :, z_idx].copy()
    sigma_slice = sigma_3d[:, :, z_idx]

    inactive_nodes = (sigma_slice < 1e4)
    V_slice[inactive_nodes] = np.nan

    fig = plt.figure(figsize=(8, 6), dpi=300)
    X, Y = np.meshgrid(x_coords, y_coords, indexing='ij')

    valid_v = V_slice[~np.isnan(V_slice)]
    if len(valid_v) == 0:
        vmin, vmax = 0.0, 1.0
    else:
        vmin, vmax = np.nanmin(valid_v), np.nanmax(valid_v)
        if vmin == vmax:
            vmax = vmin + 1.0

    c = plt.pcolormesh(X, Y, V_slice, cmap='viridis', shading='nearest', vmin=vmin, vmax=vmax)

    ax = fig.gca()
    ax.set_title(f'MNA Solver Potential Map (Z = {target_z} mm)', fontsize=14, pad=15)
    ax.set_xlabel('X [mm]', fontsize=12)
    ax.set_ylabel('Y [mm]', fontsize=12)

    fmt = ticker.ScalarFormatter(useOffset=False)
    fmt.set_scientific(False)

    cbar = fig.colorbar(c, ax=ax, shrink=0.6, pad=0.1, format=fmt)
    cbar.set_label('Voltage [V]', rotation=270, labelpad=15, fontsize=12)

    fig.tight_layout()
    fig.savefig(filename)
    plt.close(fig)


def plot_3d_potential(potential_3d, x_coords, y_coords, z_coords, sigma_map, output_filename):
    fig = plt.figure(figsize=(10, 8))
    ax = fig.add_subplot(111, projection='3d')

    if sigma_map is not None:
        copper_mask = sigma_map > 1e5  
    else:
        copper_mask = ~np.isnan(potential_3d)

    vmax = np.nanmax(potential_3d)
    if np.isnan(vmax):
        vmax = 1.0  
    norm = plt.Normalize(vmin=0.0, vmax=vmax)
    cmap = plt.colormaps.get_cmap('viridis')

    safe_potential = np.nan_to_num(potential_3d, nan=0.0)
    colors = cmap(norm(safe_potential))

    ax.voxels(copper_mask, facecolors=colors, edgecolor='none')

    ax.invert_yaxis()

    if x_coords is not None and len(x_coords) > 0:
        ax.set_xticks(np.linspace(0, len(x_coords) - 1, num=5))
        ax.set_xticklabels(np.round(np.linspace(x_coords[0], x_coords[-1], num=5), 2))
        ax.set_xlabel('X (mm)')

    if y_coords is not None and len(y_coords) > 0:
        ax.set_yticks(np.linspace(0, len(y_coords) - 1, num=5))
        ax.set_yticklabels(np.round(np.linspace(y_coords[0], y_coords[-1], num=5), 2))
        ax.set_ylabel('Y (mm)')

    if z_coords is not None and len(z_coords) > 0:
        ax.set_zticks(np.linspace(0, len(z_coords) - 1, num=5))
        ax.set_zticklabels(np.round(np.linspace(z_coords[0], z_coords[-1], num=5), 2))
        ax.set_zlabel('Z (mm)')

    sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm)
    sm.set_array([])
    fig.colorbar(sm, ax=ax, label='Potential (V)', shrink=0.7, pad=0.1)

    if x_coords is not None and y_coords is not None and z_coords is not None:
        ax.set_box_aspect((np.ptp(x_coords), np.ptp(y_coords), np.ptp(z_coords)))

    plt.savefig(output_filename, format='png', bbox_inches='tight', dpi=150)
    plt.close(fig)

def plot_power_density(potential_3d, x_coords, y_coords, z_coords, sigma_map, target_z, sigma_copper=58e6, filename='power_density_map.png'):
    Nx, Ny, Nz = len(x_coords), len(y_coords), len(z_coords)
    z_idx = np.argmin(np.abs(z_coords - target_z))

    x_m = x_coords * 1e-3
    y_m = y_coords * 1e-3
    z_m = z_coords * 1e-3

    dz = np.diff(z_m)
    if len(dz) > 0 and np.any(dz == 0):
        z_m = z_m + np.arange(len(z_m)) * 1e-9  

    Ex, Ey, Ez = np.gradient(potential_3d, x_m, y_m, z_m, edge_order=1)
    E_mag_sq = Ex[:, :, z_idx] ** 2 + Ey[:, :, z_idx] ** 2 + Ez[:, :, z_idx] ** 2

    P_density = sigma_copper * E_mag_sq

    sigma_3d = sigma_map.reshape((Nx, Ny, Nz), order="C")
    sigma_slice = sigma_3d[:, :, z_idx]
    P_density[sigma_slice < 1e4] = np.nan

    P_density[np.isinf(P_density)] = np.nan

    valid_p = P_density[~np.isnan(P_density)]
    if len(valid_p) > 0:
        p98 = np.nanpercentile(valid_p, 98)
        P_density[P_density > p98] = p98

    plt.figure(figsize=(8, 6), dpi=300)
    X, Y = np.meshgrid(x_coords, y_coords, indexing='ij')

    c = plt.pcolormesh(X, Y, P_density, cmap='inferno', shading='nearest')

    fmt = ticker.ScalarFormatter(useOffset=False)
    fmt.set_scientific(True)

    cbar = plt.colorbar(c, format=fmt)
    cbar.set_label('Power Density [W/m³]', rotation=270, labelpad=15, fontsize=12)

    plt.title(f'Power Density Distribution (Z = {target_z} mm)', fontsize=14, pad=15)
    plt.xlabel('X Coordinate [mm]', fontsize=12)
    plt.ylabel('Y Coordinate [mm]', fontsize=12)

    plt.tight_layout()
    plt.savefig(filename)
    plt.close()


def plot_dc_ir_drop_profile(potential_3d, x_coords, y_coords, z_coords, sigma_map, target_z, target_y, metadata_path, network_name, output_filename):
    load_current = 1.0
    if metadata_path and os.path.exists(metadata_path):
        try:
            with open(metadata_path, 'r', encoding='utf-8') as f:
                meta = json.load(f)
                load_current = meta.get("dc_analysis", {}).get(network_name, {}).get("load_sink", {}).get("current_A", 1.0)
        except Exception:
            pass

    if sigma_map is not None:
        copper_mask = sigma_map > 1e4
    else:
        copper_mask = ~np.isnan(potential_3d)

    all_potentials = potential_3d[copper_mask]

    if len(all_potentials) == 0:
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.text(0.5, 0.5, "No conductive copper nodes found in the simulation.", ha='center', va='center')
        plt.savefig(output_filename, format='png', bbox_inches='tight', dpi=150)
        plt.close(fig)
        return

    v_max = np.nanmax(all_potentials)
    threshold = v_max / 2.0

    v_vcc = all_potentials[all_potentials > threshold]
    v_gnd = all_potentials[all_potentials <= threshold]

    vcc_sorted = np.sort(v_vcc)[::-1]
    gnd_sorted = np.sort(v_gnd)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), sharex=False)
    fig.suptitle('Global DC IR Drop Waterfall Profile', fontsize=14, fontweight='bold')

    if len(vcc_sorted) > 0:
        x_vcc_nodes = np.arange(len(vcc_sorted))
        ax1.plot(x_vcc_nodes, vcc_sorted, color='#d62728', linewidth=2, label='Power Net (VCC)')
        ax1.fill_between(x_vcc_nodes, vcc_sorted, np.min(vcc_sorted), color='#d62728', alpha=0.2)

        ax1.set_ylabel('Potential (V)', color='#d62728', fontweight='bold')
        ax1.tick_params(axis='y', labelcolor='#d62728')
        ax1.grid(True, linestyle='--', alpha=0.7)

        vcc_drop_mv = (np.max(vcc_sorted) - np.min(vcc_sorted)) * 1000
        ax1.set_title(f'Power Net Drop: {vcc_drop_mv:.2f} mV (Load: {load_current}A)', fontsize=11)
        ax1.set_xticks([])
    else:
        ax1.text(0.5, 0.5, 'No Power Net data available', ha='center', va='center', transform=ax1.transAxes)

    if len(gnd_sorted) > 0:
        x_gnd_nodes = np.arange(len(gnd_sorted))
        ax2.plot(x_gnd_nodes, gnd_sorted, color='#1f77b4', linewidth=2, label='Ground Net (GND)')
        ax2.fill_between(x_gnd_nodes, gnd_sorted, np.min(gnd_sorted), color='#1f77b4', alpha=0.2)

        ax2.set_ylabel('Potential (V)', color='#1f77b4', fontweight='bold')
        ax2.tick_params(axis='y', labelcolor='#1f77b4')
        ax2.grid(True, linestyle='--', alpha=0.7)

        gnd_bounce_mv = (np.max(gnd_sorted) - np.min(gnd_sorted)) * 1000
        ax2.set_title(f'Ground Bounce / Return Drop: {gnd_bounce_mv:.2f} mV', fontsize=11)
        ax2.set_xticks([])
    else:
        ax2.text(0.5, 0.5, 'No Ground Net data available', ha='center', va='center', transform=ax2.transAxes)

    ax2.set_xlabel('Path Topology (Source → Sink)', fontweight='bold')
    plt.tight_layout()

    plt.savefig(output_filename, format='png', bbox_inches='tight', dpi=150)
    plt.close(fig)

def analyze_via_ampacity(potential_3d, x_coords, y_coords, z_coords, sigma_map, via_bounds, target_z=0.5, max_amps=1.5):
    Nx, Ny, Nz = len(x_coords), len(y_coords), len(z_coords)
    x_m, y_m, z_m = x_coords * 1e-3, y_coords * 1e-3, z_coords * 1e-3
    dx_eff, dy_eff = _get_eff_length(x_m), _get_eff_length(y_m)
    DX_2d, DY_2d = np.meshgrid(dx_eff, dy_eff, indexing='ij')

    k = np.argmin(np.abs(z_coords - target_z))
    if k == Nz - 1: k -= 1

    sig = sigma_map.reshape((Nx, Ny, Nz), order="C")
    sig_k, sig_k1 = sig[:, :, k], sig[:, :, k + 1]

    sig_eff_z = np.zeros_like(sig_k)
    valid = (sig_k > 0) & (sig_k1 > 0)
    sig_eff_z[valid] = 2.0 * sig_k[valid] * sig_k1[valid] / (sig_k[valid] + sig_k1[valid])

    Gz_k = sig_eff_z * (DX_2d * DY_2d) / (z_m[k + 1] - z_m[k])

    Iz_face = Gz_k * (potential_3d[:, :, k] - potential_3d[:, :, k + 1])
    Jz_2d = Iz_face / (DX_2d * DY_2d)

    X, Y = np.meshgrid(x_coords, y_coords, indexing='ij')
    xmin, xmax, ymin, ymax = via_bounds
    mask = (X >= xmin) & (X <= xmax) & (Y >= ymin) & (Y <= ymax)

    total_current_abs = np.abs(np.sum(Iz_face[mask]))

    print(f"\n[*] Via Ampacity Analysis at Z = {target_z} mm")
    print(f"    Via Footprint: X:[{xmin}, {xmax}], Y:[{ymin}, {ymax}]")
    print(f"    Calculated Current: {total_current_abs:.4f} A")
    if total_current_abs > max_amps:
        print(f"    [!] WARNING: Current exceeds the allowed limit ({max_amps} A)!")
    else:
        print(f"    [+] Current is within safe limits.")

    plt.figure(figsize=(7, 6))
    padding = 2.0
    mesh = plt.pcolormesh(x_coords, y_coords, np.abs(Jz_2d).T, shading='auto', cmap='viridis')
    fmt = ticker.ScalarFormatter(useOffset=False)
    fmt.set_scientific(False)

    cbar = plt.colorbar(mesh, format=fmt)
    cbar.set_label('Vertical Current Density |J_z| [A/m²]', rotation=270, labelpad=15)

    rect = patches.Rectangle((xmin, ymin), xmax - xmin, ymax - ymin,
                             linewidth=2, edgecolor='red', facecolor='none', linestyle='--')
    plt.gca().add_patch(rect)
    plt.xlim(xmin - padding, xmax + padding)
    plt.ylim(ymin - padding, ymax + padding)
    plt.title(f'Via Current Density (|I| = {total_current_abs:.4f} A)', fontsize=14, pad=12)
    plt.xlabel('X [mm]', fontsize=11)
    plt.ylabel('Y [mm]', fontsize=11)
    plt.tight_layout()
    plt.savefig('via_ampacity_analysis.png', dpi=300)
    plt.show()

    return total_current_abs


def plot_3d_sigma(sigma_map, x_coords, y_coords, z_coords, filename="debug_sigma_3d.png"):
    fig = plt.figure(figsize=(10, 8), dpi=300)
    ax = fig.add_subplot(111, projection='3d')

    Nx, Ny, Nz = len(x_coords), len(y_coords), len(z_coords)
    sigma_3d = sigma_map.reshape((Nx, Ny, Nz), order="C")

    mask = sigma_3d > 1e3

    X, Y, Z = np.meshgrid(x_coords, y_coords, z_coords, indexing='ij')

    sc = ax.scatter(X[mask], Y[mask], Z[mask], c=sigma_3d[mask],
                    cmap='copper', marker='s', s=5, alpha=0.8)

    ax.set_title('3D Sigma Map (Conductive Nodes Only)', fontsize=14, pad=15)
    ax.set_xlabel('X [mm]', fontsize=10)
    ax.set_ylabel('Y [mm]', fontsize=10)
    ax.set_zlabel('Z [mm]', fontsize=10)

    cbar = fig.colorbar(sc, ax=ax, shrink=0.6, pad=0.1)
    cbar.set_label('Conductivity [S/m]', rotation=270, labelpad=15, fontsize=12)

    ax.view_init(elev=30, azim=-45)
    fig.tight_layout()
    fig.savefig(filename)
    plt.close(fig)
    print(f"[*] 3D Sigma Map exported to {filename}")

if __name__ == "__main__":
    pass
