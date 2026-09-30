import os, sys

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

import json
import argparse
import numpy as np
from core.csx_handler import CSXGeometryLogger, ContinuousStructure, AppCSXCAD_BIN
from core.model_builder import SimulationConfig, MeshManager, GeometryBuilder, to_csx
from electrostatic_solver import ElectrostaticSolver


class DCAnalysisEngine:
    def __init__(self, json_path="simulation_metadata.json"):
        print(f"[*] Initializing DC Analysis Engine with: {json_path}")
        self.json_path = json_path
        self.config = SimulationConfig(json_path)

        # Override PEC usage for DC Analysis (MUST have finite conductivity)
        if hasattr(self.config, 'use_pec_copper'):
            self.config.use_pec_copper = False

        self.CSX = CSXGeometryLogger(ContinuousStructure())
        self.mesh_mgr = MeshManager(self.config)
        self.geometry = GeometryBuilder(self.CSX, self.config, self.mesh_mgr)
        self.network_bboxes = {} 

    def get_pad_bbox(self, kiid):
        """Finds the geometric bounding box for a specific pad by its KiCAD UUID."""
        if not kiid:
            return None, None

        pads_to_search = getattr(self.config, 'all_pads', [])
        target_pad = next((p for p in pads_to_search if p.get("kiid") == kiid), None)

        # Fallback to raw data if filtered list misses it
        if not target_pad and hasattr(self.config, 'raw_data'):
            for net in self.config.raw_data.get('nets', []):
                for item in net.get('items', []):
                    if item.get('type') == 'pad' and item.get('kiid') == kiid:
                        target_pad = item
                        break

        if not target_pad:
            print(f"[!] Warning: Pad UUID {kiid} could not be resolved in the layout.")
            return None, None

        px, py = to_csx(target_pad['x'], target_pad['y'], self.config.center_x, self.config.center_y)

        shapes = target_pad.get('pad_shapes', [])
        if shapes:
            shape = shapes[0]
            sx = shape.get('size_x_mm', 1.0) / 2.0
            sy = shape.get('size_y_mm', 1.0) / 2.0
            px += shape.get('offset_x', 0.0)
            py += shape.get('offset_y', 0.0)
        else:
            sx = target_pad.get('size_mm', 1.0) / 2.0
            sy = target_pad.get('size_mm', 1.0) / 2.0

        layer = target_pad.get('layer', 'F.Cu')
        z_info = self.config.layer_z.get(layer, {})

        z_top = z_info.get('z', 0.0)
        z_bot = z_top - z_info.get('thickness', 0.0) if not self.config.is_2_5d else z_top

        start = [px - sx, py - sy, z_bot]
        stop = [px + sx, py + sy, z_top]

        # Register key points to guarantee clean grid alignment
        self.geometry.key_x_points.extend([start[0], stop[0]])
        self.geometry.key_y_points.extend([start[1], stop[1]])
        self.geometry.key_z_points.extend([start[2], stop[2]])

        return start, stop

    def build_model(self):
        print("[*] Building DC Material Properties...")
        self.geometry.build_materials_and_dielectrics()

        print("[*] Drawing Copper Traces, Zones, and Vias...")
        self.geometry.draw_copper_features()

        # Parse the new Multi-Network DC dictionary
        dc_params = self.config.raw_data.get("dc_analysis", {})

        for network_name, network_data in dc_params.items():
            load_params = network_data.get("load_sink", {})

            vrm_start, vrm_stop = self.get_pad_bbox(network_data.get("vrm_source", {}).get("pad_kiid"))
            gnd_start, gnd_stop = self.get_pad_bbox(network_data.get("gnd_sink", {}).get("pad_kiid"))
            load_tail_start, load_tail_stop = self.get_pad_bbox(load_params.get("tail_pad_kiid"))
            load_head_start, load_head_stop = self.get_pad_bbox(load_params.get("head_pad_kiid"))

            if not all([vrm_start, gnd_start, load_tail_start, load_head_start]):
                print(f"[!] Skipping Network '{network_name}': Missing bounding boxes for assigned pads.")
                continue

            self.network_bboxes[network_name] = {
                "vrm_bounds": (vrm_start, vrm_stop),
                "gnd_bounds": (gnd_start, gnd_stop),
                "load_tail_bounds": (load_tail_start, load_tail_stop),
                "load_head_bounds": (load_head_start, load_head_stop),
                "vrm_voltage": float(network_data.get("vrm_source", {}).get("voltage", 5.0)),
                "load_current": float(load_params.get("current_A", 1.0))
            }

        print("[*] Finalizing Smart Mesh for MNA Solver...")
        self.mesh_mgr.finalize_mesh(
            self.CSX,
            self.geometry.key_x_points,
            self.geometry.key_y_points,
            self.geometry.key_z_points
        )

    def extract_pad_potentials(self, potential_map, solver, start, stop):
        x_lines, y_lines, z_lines = np.array(solver.x_lines), np.array(solver.y_lines), np.array(solver.z_lines)
        eps = 1e-6
        x_idx = np.where((x_lines >= start[0] - eps) & (x_lines <= stop[0] + eps))[0]
        y_idx = np.where((y_lines >= start[1] - eps) & (y_lines <= stop[1] + eps))[0]
        z_idx = np.where((z_lines >= start[2] - eps) & (z_lines <= stop[2] + eps))[0]

        ix, iy, iz = np.meshgrid(x_idx, y_idx, z_idx, indexing='ij')
        pad_potentials = potential_map[ix, iy, iz]

        # Filter out non-conductive Air nodes to prevent diluting the average voltage
        sigma_3d = solver.sigma_map.reshape((solver.Nx, solver.Ny, solver.Nz), order="C")
        pad_sigma = sigma_3d[ix, iy, iz]

        valid_mask = (pad_sigma > 1e3) & (~np.isnan(pad_potentials))
        valid_potentials = pad_potentials[valid_mask]

        return valid_potentials if len(valid_potentials) > 0 else np.array([0.0])

    def generate_complete_dc_report(self, network_results, output_path="dc_ir_drop.json"):
        report = {"networks": {}}

        for network_name, data in network_results.items():
            v_nominal = data["v_nominal"]
            i_load = data["i_load"]
            v_vcc_sink_avg = data["v_vcc_sink_avg"]
            v_gnd_sink_avg = data["v_gnd_sink_avg"]

            vcc_drop = v_nominal - v_vcc_sink_avg
            gnd_bounce = v_gnd_sink_avg - 0.0
            total_drop = vcc_drop + gnd_bounce
            v_load_actual = v_vcc_sink_avg - v_gnd_sink_avg

            safe_i = i_load if i_load != 0 else 1e-9

            report["networks"][network_name] = {
                "system_conditions": {
                    "vrm_v_nominal": v_nominal,
                    "load_current_A": i_load
                },
                "power_net_vcc": {
                    "v_sink_avg": v_vcc_sink_avg,
                    "v_drop": vcc_drop,
                    "r_dc_ohms": vcc_drop / safe_i
                },
                "return_net_gnd": {
                    "v_sink_avg": v_gnd_sink_avg,
                    "v_bounce": gnd_bounce,
                    "r_dc_ohms": gnd_bounce / safe_i
                },
                "load_actual": {
                    "v_differential": v_load_actual,
                    "total_pdn_drop": total_drop,
                    "total_loop_r_ohms": total_drop / safe_i
                }
            }

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=4)

    def run_dc_analysis(self, json_path):
        print("\n[*] Commencing Multi-Network MNA DC IR Drop Analysis...")

        with open(json_path, 'r', encoding='utf-8') as f:
            meta = json.load(f)

        dc_conv = meta.get("dc_convergence", {})
        max_iter = int(dc_conv.get("max_iterations", 5000))
        tolerance = float(dc_conv.get("tolerance", 1e-9))

        # Safely extract the new DC directory, fallback to constructing it if missing
        fallback_dir = os.path.join(os.path.dirname(self.config.sim_dir), "dc_results")
        dc_dir = self.config.raw_data.get("dc_simulation_dir", fallback_dir)
        os.makedirs(dc_dir, exist_ok=True)

        self.build_model()
        solver = ElectrostaticSolver(self.CSX)

        for network_name, bounds in self.network_bboxes.items():
            print(f"[*] Applying Constraints for Network: {network_name}")
            solver.AddPotential(bounds["vrm_bounds"][0], bounds["vrm_bounds"][1], bounds["vrm_voltage"])
            solver.AddPotential(bounds["gnd_bounds"][0], bounds["gnd_bounds"][1], 0.0)
            solver.AddCurrentSource(
                tail_start=bounds["load_tail_bounds"][0], tail_stop=bounds["load_tail_bounds"][1],
                head_start=bounds["load_head_bounds"][0], head_stop=bounds["load_head_bounds"][1],
                current_A=bounds["load_current"]
            )

        abort_file = os.path.join(dc_dir, "abort_mna.flag")
        if os.path.exists(abort_file):
            try:
                os.remove(abort_file)
            except OSError:
                pass

        # Execute Global Solver
        potential_map = solver.Run(max_iterations=max_iter, tolerance=tolerance, abort_file=abort_file)

        # Extract localized results per network
        print("[*] Extracting comprehensive DC loop data...")
        network_results = {}
        for network_name, bounds in self.network_bboxes.items():
            v_vcc_nodes = self.extract_pad_potentials(potential_map, solver, bounds["load_tail_bounds"][0],
                                                      bounds["load_tail_bounds"][1])
            v_gnd_nodes = self.extract_pad_potentials(potential_map, solver, bounds["load_head_bounds"][0],
                                                      bounds["load_head_bounds"][1])

            network_results[network_name] = {
                "v_nominal": bounds["vrm_voltage"],
                "i_load": bounds["load_current"],
                "v_vcc_sink_avg": float(np.mean(v_vcc_nodes)),
                "v_gnd_sink_avg": float(np.mean(v_gnd_nodes))
            }

        # Save to the specific DC directory
        json_out = os.path.join(dc_dir, "dc_ir_drop.json")
        self.generate_complete_dc_report(network_results, json_out)

        cond_out = os.path.join(dc_dir, "dc_conductivity.vtk")
        solver._export_conductivity_vtk(filename=cond_out)

        vtk_out = os.path.join(dc_dir, "dc_potential.vtk")
        solver.export_vtk(vtk_out, potential_map, solver.x_lines, solver.y_lines, solver.z_lines)
        print(f"[*] 3D VTK Maps Exported successfully.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KiCad Electrodynamic & Static Studio - DC MNA Solver Engine")
    parser.add_argument("json_file", nargs="?", default="simulation_metadata.json", help="Path to JSON metadata")
    parser.add_argument("--run-dc", action="store_true", help="Execute the MNA DC IR Drop solver")
    args = parser.parse_args()

    if args.run_dc:
        try:
            engine = DCAnalysisEngine(args.json_file)
            engine.run_dc_analysis(args.json_file)
            sys.exit(0)
        except Exception as e:
            print(f"\n[!] CRITICAL DC SOLVER ERROR: {e}")
            sys.exit(1)
    else:
        print("[!] No execution flag provided. Use --run-dc to start the analysis.")
