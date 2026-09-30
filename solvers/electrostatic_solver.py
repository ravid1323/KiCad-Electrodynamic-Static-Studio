import ctypes
import os
import sys
import json
import numpy as np
from scipy.ndimage import binary_dilation
from CSXCAD import ContinuousStructure
import tempfile
import xml.etree.ElementTree as ET
from matplotlib.path import Path

class ElectrostaticSolver:
    def __init__(self, csx: ContinuousStructure, lib_path=None):
        self.csx = csx
        grid = csx.GetGrid()

        if lib_path is None:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            if sys.platform.startswith("linux"):
                lib_path = os.path.join(current_dir, "libpoisson_mna_solver.so")
            else:
                lib_path = os.path.join(current_dir, "poisson_mna_solver.dll")

        self.x_lines = np.asarray(grid.GetLines("x"), dtype=np.float64)
        self.y_lines = np.asarray(grid.GetLines("y"), dtype=np.float64)
        self.z_lines = np.asarray(grid.GetLines("z"), dtype=np.float64)

        if len(self.x_lines) < 2 or len(self.y_lines) < 2 or len(self.z_lines) < 2:
            raise ValueError("[!] Grid must have at least 2 lines per axis.")

        self.Nx = len(self.x_lines)
        self.Ny = len(self.y_lines)
        self.Nz = len(self.z_lines)
        self.total_nodes = self.Nx * self.Ny * self.Nz

        self.sigma_map = np.full(self.total_nodes, 1e-12, dtype=np.float64)
        self.eps_map = np.ones(self.total_nodes, dtype=np.float64)
        self.mu_map = np.ones(self.total_nodes, dtype=np.float64)

        self.boundary_mask = np.zeros(self.total_nodes, dtype=np.float64)
        self.boundary_values = np.zeros(self.total_nodes, dtype=np.float64)
        self.rhs_currents = np.zeros(self.total_nodes, dtype=np.float64)
        self.potential_out = np.zeros(self.total_nodes, dtype=np.float64)
        self.potential_3d_map = None

        self.priority_map = np.full(
            self.total_nodes,
            -np.inf,
            dtype=float
        )

        self.dz_eff = np.fromiter(
            (
                self._get_dz_eff(k)
                for k in range(self.Nz)
            ),
            dtype=float,
            count=self.Nz
        )

        X, Y = np.meshgrid(
            self.x_lines,
            self.y_lines,
            indexing="ij"
        )

        self._voxelize_csx()

        self._load_cpp_library(lib_path)

    def _get_dz_eff(self, k: int):
        if self.Nz == 1: return 1.0
        if k == 0: return (self.z_lines[1] - self.z_lines[0]) / 2.0
        if k == self.Nz - 1: return (self.z_lines[-1] - self.z_lines[-2]) / 2.0
        return (self.z_lines[k + 1] - self.z_lines[k - 1]) / 2.0

    def _assign_material(
            self,
            indices,
            priority,
            sigma,
            eps,
            mu
    ):
        indices = np.asarray(
            indices,
            dtype=np.intp
        )

        if indices.size == 0:
            return

        mask = (
                priority >= self.priority_map[indices]
        )

        if not np.any(mask):
            return

        selected = indices[mask]

        self.priority_map[selected] = priority
        self.sigma_map[selected] = sigma
        self.eps_map[selected] = eps
        self.mu_map[selected] = mu

    def _voxelize_csx(self):
        print("[*] Retrieving in-memory geometry from CSXWrapper...")

        materials = self.csx.GetVoxelizationMaterials()
        eps_margin = 1e-6

        def axis_indices(lines, lo, hi):
            lo, hi = min(lo, hi), max(lo, hi)

            start = np.searchsorted(
                lines,
                lo - eps_margin,
                side="left"
            )

            stop = np.searchsorted(
                lines,
                hi + eps_margin,
                side="right"
            )

            if start >= stop:
                return np.empty(0, dtype=np.intp)

            return np.arange(start, stop, dtype=np.intp)

        def make_flat_indices(ix, iy, iz):
            if ix.size == 0 or iy.size == 0 or iz.size == 0:
                return np.empty(0, dtype=np.intp)

            base_ij = (
                    ix[:, None] * self.Ny +
                    iy[None, :]
            ).ravel()

            return (
                    base_ij[:, None] * self.Nz +
                    iz[None, :]
            ).ravel()

        for obj in materials:
            sigma = float(obj["sigma"])
            eps = float(obj["eps"])
            mu = float(obj["mu"])
            thickness = float(obj["thickness"])
            priority = int(obj["priority"])

            prim = obj["primitive_data"]
            tag = prim["type"]


            if tag == "box":
                p1 = prim["start"]
                p2 = prim["stop"]

                ix = axis_indices(
                    self.x_lines,
                    p1[0],
                    p2[0]
                )

                iy = axis_indices(
                    self.y_lines,
                    p1[1],
                    p2[1]
                )

                iz = axis_indices(
                    self.z_lines,
                    p1[2],
                    p2[2]
                )

                if ix.size == 0 or iy.size == 0 or iz.size == 0:
                    continue

                indices = make_flat_indices(ix, iy, iz)

                actual_sigma = sigma

                if thickness > 0.0 and sigma > 0.0:
                    avg_dz = self.dz_eff[iz].mean() * 1e-3

                    if avg_dz > 0.0:
                        actual_sigma = sigma * thickness / avg_dz

                self._assign_material(
                    indices,
                    priority,
                    actual_sigma,
                    eps,
                    mu
                )

            elif tag == "cylinder":
                p1 = np.asarray(
                    prim["start"],
                    dtype=float
                )

                p2 = np.asarray(
                    prim["stop"],
                    dtype=float
                )

                radius = float(prim["radius"])

                ix = axis_indices(
                    self.x_lines,
                    p1[0] - radius,
                    p1[0] + radius
                )

                iy = axis_indices(
                    self.y_lines,
                    p1[1] - radius,
                    p1[1] + radius
                )

                iz = axis_indices(
                    self.z_lines,
                    p1[2],
                    p2[2]
                )

                if ix.size == 0 or iy.size == 0 or iz.size == 0:
                    continue

                Xc, Yc = np.meshgrid(
                    self.x_lines[ix],
                    self.y_lines[iy],
                    indexing="ij"
                )

                dist2 = (Xc - p1[0]) ** 2 + (Yc - p1[1]) ** 2

                inside_mask = (
                        dist2 <= (radius + eps_margin) ** 2
                )

                if not np.any(inside_mask):
                    continue

                base_ij = (
                        ix[:, None] * self.Ny +
                        iy[None, :]
                )

                inside_base = (
                        base_ij[inside_mask] * self.Nz
                )

                indices = (
                        inside_base[:, None] +
                        iz[None, :]
                ).ravel()

                self._assign_material(
                    indices,
                    priority,
                    sigma,
                    eps,
                    mu
                )

            elif tag in ("polygon", "linpoly"):

                pts_x, pts_y = prim.get(
                    "points",
                    ([], [])
                )

                if not pts_x or not pts_y:
                    continue

                pts_x = np.asarray(
                    pts_x,
                    dtype=float
                )

                pts_y = np.asarray(
                    pts_y,
                    dtype=float
                )

                x_min = pts_x.min()
                x_max = pts_x.max()
                y_min = pts_y.min()
                y_max = pts_y.max()

                ix = axis_indices(
                    self.x_lines,
                    x_min,
                    x_max
                )

                iy = axis_indices(
                    self.y_lines,
                    y_min,
                    y_max
                )

                if ix.size == 0 or iy.size == 0:
                    continue

                z_elev = float(
                    prim.get("elevation", 0.0)
                )

                length = (
                    float(prim.get("length", 0.0))
                    if tag == "linpoly"
                    else 0.0
                )

                z_min = min(
                    z_elev,
                    z_elev + length
                )

                z_max = max(
                    z_elev,
                    z_elev + length
                )

                iz = axis_indices(
                    self.z_lines,
                    z_min,
                    z_max
                )

                if iz.size == 0:
                    continue

                Xc, Yc = np.meshgrid(
                    self.x_lines[ix],
                    self.y_lines[iy],
                    indexing="ij"
                )

                candidate_points = np.column_stack(
                    (
                        Xc.ravel(),
                        Yc.ravel()
                    )
                )

                polygon = Path(
                    np.column_stack(
                        (pts_x, pts_y)
                    )
                )

                inside_mask = polygon.contains_points(
                    candidate_points,
                    radius=eps_margin
                )

                if not np.any(inside_mask):
                    continue

                local_ij = np.flatnonzero(
                    inside_mask
                )

                local_ny = iy.size

                local_i = (
                        local_ij // local_ny
                )

                local_j = (
                        local_ij % local_ny
                )

                global_i = ix[local_i]
                global_j = iy[local_j]

                base_ij = (
                        global_i * self.Ny +
                        global_j
                )

                base_ij *= self.Nz

                actual_sigma = sigma

                if thickness > 0.0 and sigma > 0.0:
                    avg_dz = self.dz_eff[iz].mean() * 1e-3

                    if avg_dz > 0.0:
                        actual_sigma = (
                                sigma * thickness / avg_dz
                        )

                indices = (
                        base_ij[:, None] +
                        iz[None, :]
                ).ravel()

                self._assign_material(
                    indices,
                    priority,
                    actual_sigma,
                    eps,
                    mu
                )

        print(
            "[+] Direct Voxelization complete. "
            f"Active conductive nodes: "
            f"{np.count_nonzero(self.sigma_map >= 1e3):,}"
        )

    def _get_indices_in_box(self, start_xyz: list[float], stop_xyz: list[float], eps=1e-9):
        min_x, max_x = min(start_xyz[0], stop_xyz[0]) - eps, max(start_xyz[0], stop_xyz[0]) + eps
        min_y, max_y = min(start_xyz[1], stop_xyz[1]) - eps, max(start_xyz[1], stop_xyz[1]) + eps
        min_z, max_z = min(start_xyz[2], stop_xyz[2]) - eps, max(start_xyz[2], stop_xyz[2]) + eps

        x_idx = np.where((self.x_lines >= min_x) & (self.x_lines <= max_x))[0]
        y_idx = np.where((self.y_lines >= min_y) & (self.y_lines <= max_y))[0]
        z_idx = np.where((self.z_lines >= min_z) & (self.z_lines <= max_z))[0]

        if x_idx.size == 0 or y_idx.size == 0 or z_idx.size == 0:
            return []

        base_ij = (
                          x_idx[:, None] * self.Ny +
                          y_idx[None, :]
                  ).ravel() * self.Nz

        indices = (
                base_ij[:, None] +
                z_idx[None, :]
        ).ravel()

        return indices.tolist()

    def AddPotential(self, coord_start, coord_stop, potential_V):
        """
        Force Dirichlet condition (V) in a cell or specific object
        Used to define a voltage potentioal between two coordinates
        """
        indices = self._get_indices_in_box(coord_start, coord_stop)

        # Filter to only include highly conductive nodes
        valid_indices = [idx for idx in indices if self.sigma_map[idx] > 1e3]

        if not valid_indices:
            print(f"[!] Warning: Potential bounding box {coord_start}-{coord_stop} contains no conductive nodes.")
            return

        for idx in valid_indices:
            self.boundary_mask[idx] = 1.0
            self.boundary_values[idx] = float(potential_V)

        print(f"[*] Added Potential Constraint: {potential_V}V (applied to {len(valid_indices)} nodes).")

    def AddCurrentSource(self, tail_start: list[float], tail_stop: list[float], head_start: list[float],
                         head_stop: list[float], current_A: float):
        """
        Assigns an ideal current source/sink boundary condition between two specified regions.
        """
        tail_indices = self._get_indices_in_box(tail_start, tail_stop)
        head_indices = self._get_indices_in_box(head_start, head_stop)

        current_A = float(current_A)

        # Filter out air/dielectric nodes BEFORE distributing current
        valid_tail = [idx for idx in tail_indices if self.sigma_map[idx] > 1e3]
        valid_head = [idx for idx in head_indices if self.sigma_map[idx] > 1e3]

        if valid_tail:
            rhs_per_node = current_A / len(valid_tail)
            for idx in valid_tail:
                self.rhs_currents[idx] -= rhs_per_node

        if valid_head:
            rhs_per_node = current_A / len(valid_head)
            for idx in valid_head:
                self.rhs_currents[idx] += rhs_per_node

        print(f"[*] Added Current Source: {current_A}A (Tail nodes: {len(valid_tail)}, Head nodes: {len(valid_head)}).")

    def _load_cpp_library(self, lib_path: str):
        lib_path = os.path.abspath(lib_path)
        if not os.path.exists(lib_path):
            raise FileNotFoundError(f"[!] Cannot find C++ solver DLL at {lib_path}")

        if hasattr(os, "add_dll_directory"):
            ucrt64_bin = r"C:\msys64\ucrt64\bin"
            if os.path.isdir(ucrt64_bin):
                try: os.add_dll_directory(ucrt64_bin)
                except OSError: pass
            try: os.add_dll_directory(os.path.dirname(lib_path))
            except OSError: pass

        self.lib = ctypes.CDLL(lib_path)
        array_1d_double = np.ctypeslib.ndpointer(dtype=np.float64, ndim=1, flags="C_CONTIGUOUS")

        # Existing Solver Binding Only
        self.lib.run_poisson_mna_solver.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int,
            array_1d_double, array_1d_double, array_1d_double,
            array_1d_double, array_1d_double, array_1d_double,
            array_1d_double, array_1d_double, array_1d_double,
            np.ctypeslib.ndpointer(dtype=np.float64, ndim=1, flags="C_CONTIGUOUS"),
            ctypes.c_int, ctypes.c_double, ctypes.c_char_p
        ]
        self.lib.run_poisson_mna_solver.restype = None

    def Run(self, max_iterations=5000, tolerance=1e-9, abort_file=""):
        # Geometry is already voxelized in __init__ (before AddPotential/AddCurrentSource
        # were called), so sigma_map/eps_map/mu_map are already current here.
        print(f"[*] Starting MNA-PCG solver on grid ({self.Nx} x {self.Ny} x {self.Nz})")
        abort_bytes = abort_file.encode('utf-8') if abort_file else None

        self.lib.run_poisson_mna_solver(
            self.Nx, self.Ny, self.Nz,
            np.ascontiguousarray(self.x_lines * 1e-3),
            np.ascontiguousarray(self.y_lines * 1e-3),
            np.ascontiguousarray(self.z_lines * 1e-3),
            np.ascontiguousarray(self.sigma_map),
            np.ascontiguousarray(self.eps_map),
            np.ascontiguousarray(self.mu_map),
            np.ascontiguousarray(self.boundary_mask),
            np.ascontiguousarray(self.boundary_values),
            np.ascontiguousarray(self.rhs_currents),
            self.potential_out,
            int(max_iterations), float(tolerance),
            abort_bytes
        )

        self.potential_3d_map = self.potential_out.reshape((self.Nx, self.Ny, self.Nz), order="C")
        mask_3d = self.boundary_mask.reshape((self.Nx, self.Ny, self.Nz), order="C")
        values_3d = self.boundary_values.reshape((self.Nx, self.Ny, self.Nz), order="C")
        self.potential_3d_map[mask_3d > 0.5] = values_3d[mask_3d > 0.5]
        self.potential_3d_map = self.potential_3d_map.round(abs(int(np.log10(tolerance))))
        print("[+] 3D Potential field successfully solved and voxelized.")
        return self.potential_3d_map

    def export_dc_ir_json(self, filename: str, v_nominal: float, i_sink: float, sink_start: list[float],
                          sink_stop: list[float]):
        if self.potential_3d_map is None:
            raise RuntimeError("[!] Run solver before exporting.")

        sink_indices = self._get_indices_in_box(sink_start, sink_stop)
        if not sink_indices:
            raise ValueError("[!] Sink bounding box is empty.")

        # Do not sample air nodes when calculating the average voltage at the pad!
        valid_sink = [idx for idx in sink_indices if self.sigma_map[idx] >= 1e3]
        if not valid_sink:
            valid_sink = sink_indices

        v_sink_avg = float(np.mean(self.potential_out[valid_sink]))
        v_drop = v_nominal - v_sink_avg
        r_dc = v_drop / float(i_sink)

        dc_data = {"v_nominal": float(v_nominal), "v_sink_avg": v_sink_avg, "v_drop": v_drop, "i_sink": float(i_sink),
                   "r_dc": r_dc}
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(dc_data, f, indent=4)

        print(f"[*] DC IR JSON exported to {filename} (V_drop: {v_drop:.4g} V, R_dc: {r_dc:.4g} Ohms)")

    def export_vtk(self, filename, potential_3d, x_coords, y_coords, z_coords):
        nx, ny, nz = potential_3d.shape
        with open(filename, 'w') as f:
            f.write("# vtk DataFile Version 3.0\nElectrostatic Potential Field\nASCII\nDATASET RECTILINEAR_GRID\n")
            f.write(f"DIMENSIONS {nx} {ny} {nz}\n")
            f.write(f"X_COORDINATES {nx} double\n" + " ".join(map(str, x_coords)) + "\n")
            f.write(f"Y_COORDINATES {ny} double\n" + " ".join(map(str, y_coords)) + "\n")
            f.write(f"Z_COORDINATES {nz} double\n" + " ".join(map(str, z_coords)) + "\n")
            f.write(f"POINT_DATA {nx * ny * nz}\nSCALARS Potential double 1\nLOOKUP_TABLE default\n")
            for val in potential_3d.flatten(order='F'):
                f.write(f"{val}\n")

    def _export_conductivity_vtk(self, filename="conductivity.vtk"):
        nx, ny, nz = self.Nx, self.Ny, self.Nz
        sigma_3d = self.sigma_map.reshape((nx, ny, nz), order="C")

        with open(filename, 'w') as f:
            f.write("# vtk DataFile Version 3.0\n")
            f.write("Electrical Conductivity Field\n")
            f.write("ASCII\n")
            f.write("DATASET RECTILINEAR_GRID\n")
            f.write(f"DIMENSIONS {nx} {ny} {nz}\n")

            f.write(f"X_COORDINATES {nx} double\n")
            f.write(" ".join(map(str, self.x_lines)) + "\n")

            f.write(f"Y_COORDINATES {ny} double\n")
            f.write(" ".join(map(str, self.y_lines)) + "\n")

            f.write(f"Z_COORDINATES {nz} double\n")
            f.write(" ".join(map(str, self.z_lines)) + "\n")

            total_points = nx * ny * nz
            f.write(f"POINT_DATA {total_points}\n")
            f.write("SCALARS Conductivity double 1\n")
            f.write("LOOKUP_TABLE default\n")

            flattened_sigma = sigma_3d.flatten(order='F')
            for val in flattened_sigma:
                f.write(f"{val}\n")
