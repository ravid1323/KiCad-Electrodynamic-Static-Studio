// poisson_mna_solver.cpp
//
// Universal 3D Nodal Analysis (MNA) Solver.
// Accepts non-uniform grid coordinates (x, y, z) and full 3D material property
// matrices (sigma, eps, mu) from Python.
//
// Currently implements the DC IR-Drop (Poisson) solver:
//      div(sigma * grad(V)) = -I
// 
// The eps_map and mu_map are received to maintain a generic unified API 
// for future quasi-static AC/Harmonic extensions.

#include <iostream>
#include <vector>
#include <cmath>
#include <algorithm>
#include <limits>
#include <fstream>
#include <ctime>

#ifdef _WIN32
    #define EXPORT_API __declspec(dllexport)
#else
    #define EXPORT_API __attribute__((visibility("default")))
#endif

class UniversalMNASolver {
private:
    int Nx, Ny, Nz;
    int total_nodes;

    // Grid coordinates (assumed to be in meters when passed from Python)
    std::vector<double> x_lines;
    std::vector<double> y_lines;
    std::vector<double> z_lines;

    // 3D Material Matrices
    std::vector<double> sigma_map;
    std::vector<double> eps_map;
    std::vector<double> mu_map;

    // Boundary and Source conditions
    std::vector<double> boundary_mask;
    std::vector<double> boundary_values;
    std::vector<double> rhs_currents;
    std::vector<double> potential;

    // Pre-calculated Conductance edges for fast sparse matrix multiplication
    // Gx[p] is the conductance between node p(i,j,k) and p_next(i+1,j,k)
    std::vector<double> Gx;
    std::vector<double> Gy;
    std::vector<double> Gz;
    std::vector<double> diag_G; // Main diagonal of the matrix

    // PCG Vectors
    std::vector<double> residual;
    std::vector<double> direction;
    std::vector<double> preconditioned;
    std::vector<double> Ap;

    inline int index(int i, int j, int k) const {
        return (i * Ny * Nz) + (j * Nz) + k;
    }

	// Calculation effective crossection of a cell around a specific node
    double get_eff_length(const std::vector<double>& lines, int idx, int max_idx) const {
        if (max_idx == 0) return 1.0; // 2D fallback
        if (idx == 0) return (lines[1] - lines[0]) / 2.0;
        if (idx == max_idx) return (lines[max_idx] - lines[max_idx - 1]) / 2.0;
        return (lines[idx + 1] - lines[idx - 1]) / 2.0;
    }

	// Function for preparing the resistive grid  (MNA Matrix)
	// avoiding re-calculation of area and distance in every iteration of the PCG
    void build_conductance_network() {
        std::fill(Gx.begin(), Gx.end(), 0.0);
        std::fill(Gy.begin(), Gy.end(), 0.0);
        std::fill(Gz.begin(), Gz.end(), 0.0);
        std::fill(diag_G.begin(), diag_G.end(), 0.0);

        for (int i = 0; i < Nx; ++i) {
            double dy_eff = get_eff_length(y_lines, 0, Ny - 1); // Will be updated in inner loop if needed
            for (int j = 0; j < Ny; ++j) {
                dy_eff = get_eff_length(y_lines, j, Ny - 1);
                for (int k = 0; k < Nz; ++k) {
                    double dz_eff = get_eff_length(z_lines, k, Nz - 1);
                    double dx_eff = get_eff_length(x_lines, i, Nx - 1);
                    
                    int p = index(i, j, k);
                    double sig_p = sigma_map[p];

                    // Conductance in +X direction
                    if (i < Nx - 1) {
                        double sig_next = sigma_map[index(i + 1, j, k)];
                        double sig_eff = (sig_p > 0 && sig_next > 0) ? (2.0 * sig_p * sig_next) / (sig_p + sig_next) : 0.0;
                        double dist = x_lines[i + 1] - x_lines[i];
                        Gx[p] = (dist > 0) ? sig_eff * (dy_eff * dz_eff) / dist : 0.0;
                    }

                    // Conductance in +Y direction
                    if (j < Ny - 1) {
                        double sig_next = sigma_map[index(i, j + 1, k)];
                        double sig_eff = (sig_p > 0 && sig_next > 0) ? (2.0 * sig_p * sig_next) / (sig_p + sig_next) : 0.0;
                        double dist = y_lines[j + 1] - y_lines[j];
                        Gy[p] = (dist > 0) ? sig_eff * (dx_eff * dz_eff) / dist : 0.0;
                    }

                    // Conductance in +Z direction
                    if (k < Nz - 1) {
                        double sig_next = sigma_map[index(i, j, k + 1)];
                        double sig_eff = (sig_p > 0 && sig_next > 0) ? (2.0 * sig_p * sig_next) / (sig_p + sig_next) : 0.0;
                        double dist = z_lines[k + 1] - z_lines[k];
                        Gz[p] = (dist > 0) ? sig_eff * (dx_eff * dy_eff) / dist : 0.0;
                    }
                }
            }
        }

        // Calculating main diagnal of the matrix
        for (int i = 0; i < Nx; ++i) {
            for (int j = 0; j < Ny; ++j) {
                for (int k = 0; k < Nz; ++k) {
                    int p = index(i, j, k);
                    if (boundary_mask[p] > 0.5) {
                        diag_G[p] = 1.0;
                        continue;
                    }

                    double diag_sum = 0.0;
                    if (i > 0)      diag_sum += Gx[index(i - 1, j, k)];
                    if (i < Nx - 1) diag_sum += Gx[p];
                    if (j > 0)      diag_sum += Gy[index(i, j - 1, k)];
                    if (j < Ny - 1) diag_sum += Gy[p];
                    if (k > 0)      diag_sum += Gz[index(i, j, k - 1)];
                    if (k < Nz - 1) diag_sum += Gz[p];

                    double min_shunt = 1e-12;
                    diag_G[p] = (diag_sum > 0.0) ? diag_sum + min_shunt : 1.0;
                }
            }
        }
    }

    void apply_operator(const std::vector<double>& x, std::vector<double>& y) const {
        std::fill(y.begin(), y.end(), 0.0);
        
        for (int i = 0; i < Nx; ++i) {
            for (int j = 0; j < Ny; ++j) {
                for (int k = 0; k < Nz; ++k) {
                    int p = index(i, j, k);

                    if (boundary_mask[p] > 0.5) {
                        y[p] = x[p]; // dirichlet ancor
                        continue;
                    }

                    double center_val = 0.0;
                    double off_diag_sum = 0.0;

                    // X Axis
                    if (i > 0) {
                        double G = Gx[index(i - 1, j, k)];
                        center_val += G; off_diag_sum += G * x[index(i - 1, j, k)];
                    }
                    if (i < Nx - 1) {
                        double G = Gx[p];
                        center_val += G; off_diag_sum += G * x[index(i + 1, j, k)];
                    }
                    // Y Axis
                    if (j > 0) {
                        double G = Gy[index(i, j - 1, k)];
                        center_val += G; off_diag_sum += G * x[index(i, j - 1, k)];
                    }
                    if (j < Ny - 1) {
                        double G = Gy[p];
                        center_val += G; off_diag_sum += G * x[index(i, j + 1, k)];
                    }
                    // Z Axis
                    if (k > 0) {
                        double G = Gz[index(i, j, k - 1)];
                        center_val += G; off_diag_sum += G * x[index(i, j, k - 1)];
                    }
                    if (k < Nz - 1) {
                        double G = Gz[p];
                        center_val += G; off_diag_sum += G * x[index(i, j, k + 1)];
                    }

                    double min_shunt = 1e-12;
                    y[p] = ((center_val + min_shunt) * x[p]) - off_diag_sum;
                }
            }
        }
    }

    void build_system_rhs(std::vector<double>& system_rhs) const {
        system_rhs = rhs_currents;
    }

    void jacobi_precondition(const std::vector<double>& in, std::vector<double>& out) const {
        for (int p = 0; p < total_nodes; ++p) {
            out[p] = (boundary_mask[p] > 0.5) ? 0.0 : in[p] / diag_G[p];
        }
    }

    double dot_free(const std::vector<double>& a, const std::vector<double>& b) const {
        double sum = 0.0;
        for (int p = 0; p < total_nodes; ++p) {
            if (boundary_mask[p] <= 0.5) {
                sum += a[p] * b[p];
            }
        }
        return sum;
    }

public:
    UniversalMNASolver(
        int nx, int ny, int nz,
        const double* x_in, const double* y_in, const double* z_in,
        const double* sig_in, const double* eps_in, const double* mu_in,
        const double* mask_in, const double* val_in, const double* cur_in
    ) : Nx(nx), Ny(ny), Nz(nz), total_nodes(nx * ny * nz) {

        if (Nx < 2 || Ny < 2 || Nz < 2) {
            throw std::runtime_error("MNA solver requires Nx, Ny, Nz >= 2.");
        }

        x_lines.assign(x_in, x_in + Nx);
        y_lines.assign(y_in, y_in + Ny);
        z_lines.assign(z_in, z_in + Nz);

        sigma_map.assign(sig_in, sig_in + total_nodes);
        eps_map.assign(eps_in, eps_in + total_nodes);
        mu_map.assign(mu_in, mu_in + total_nodes);

        boundary_mask.assign(mask_in, mask_in + total_nodes);
        boundary_values.assign(val_in, val_in + total_nodes);
        rhs_currents.assign(cur_in, cur_in + total_nodes);
        
        potential.resize(total_nodes, 0.0);
        for (int p = 0; p < total_nodes; ++p) {
            if (boundary_mask[p] > 0.5) potential[p] = boundary_values[p];
        }

        Gx.resize(total_nodes, 0.0);
        Gy.resize(total_nodes, 0.0);
        Gz.resize(total_nodes, 0.0);
        diag_G.resize(total_nodes, 0.0);

        residual.resize(total_nodes, 0.0);
        direction.resize(total_nodes, 0.0);
        preconditioned.resize(total_nodes, 0.0);
        Ap.resize(total_nodes, 0.0);

        build_conductance_network();
    }

void solve(int max_iterations = 5000, double tolerance = 1e-9, const char* abort_file = nullptr) {        std::vector<double> system_rhs;
        build_system_rhs(system_rhs);

        apply_operator(potential, Ap);

        for (int p = 0; p < total_nodes; ++p) {
            residual[p] = (boundary_mask[p] > 0.5) ? 0.0 : system_rhs[p] - Ap[p];
        }

        jacobi_precondition(residual, preconditioned);

        double rz_old = dot_free(residual, preconditioned);
        if (!std::isfinite(rz_old) || std::abs(rz_old) < 1e-30) {
            std::cout << "[*] MNA solver: initial residual is already small.\n";
            return;
        }

        direction = preconditioned;

        double initial_norm = std::sqrt(std::max(dot_free(residual, residual), 0.0));
        std::cout << "[*] Starting MNA-PCG solver: " << Nx << "x" << Ny << "x" << Nz
                  << ", initial residual = " << initial_norm << "\n";

        for (int iter = 0; iter < max_iterations; ++iter) {
            apply_operator(direction, Ap);

            const double denom = dot_free(direction, Ap);
            if (!std::isfinite(denom) || std::abs(denom) < 1e-30) {
                std::cerr << "[!] PCG breakdown: invalid search-direction denominator.\n";
                break;
            }

            const double alpha = rz_old / denom;

            for (int p = 0; p < total_nodes; ++p) {
                if (boundary_mask[p] <= 0.5) {
                    potential[p] += alpha * direction[p];
                    residual[p] -= alpha * Ap[p];
                } else {
                    potential[p] = boundary_values[p];
                }
            }

            const double residual_norm = std::sqrt(std::max(dot_free(residual, residual), 0.0));
            const double rel_residual = residual_norm / std::max(1.0, initial_norm);

            // Single check for logging, timestamping, and aborting
            if (iter % 100 == 0 || residual_norm < tolerance) {
                // Generate formatted timestamp (HH:MM:SS) using global C functions
                time_t now = time(nullptr);
                char time_buf[10];
                strftime(time_buf, sizeof(time_buf), "%H:%M:%S", localtime(&now));

                std::cout << "[" << time_buf << "]   -> Iteration " << iter 
                          << ", Abs Res = " << residual_norm 
                          << ", Rel Res = " << rel_residual << "\n";

                // --- ABORT FLAG CHECK ---
                if (abort_file && abort_file[0] != '\0') {
                    std::ifstream f(abort_file);
                    if (f.good()) {
                        std::cerr << "\n[!] MNA-PCG solver aborted by user flag.\n";
                        return; // Gracefully break the solver loop
                    }
                }
            }

            if (residual_norm <= tolerance * std::max(1.0, initial_norm)) {
                std::cout << "[*] MNA-PCG solver converged at iteration " << iter << ".\n";
                return;
            }

            jacobi_precondition(residual, preconditioned);
            const double rz_new = dot_free(residual, preconditioned);

            if (!std::isfinite(rz_new) || std::abs(rz_new) < 1e-30) {
                std::cerr << "[!] PCG breakdown: invalid preconditioned residual.\n";
                break;
            }

            const double beta = rz_new / rz_old;
            for (int p = 0; p < total_nodes; ++p) {
                direction[p] = (boundary_mask[p] <= 0.5) ? preconditioned[p] + beta * direction[p] : 0.0;
            }
            rz_old = rz_new;
			if (iter % 100 == 0 || residual_norm < tolerance) {
                std::cout << "  -> Iteration " << iter 
                          << ", Abs Res = " << residual_norm 
                          << ", Rel Res = " << rel_residual << "\n"<< std::flush;
                          
                // --- ABORT FLAG CHECK ---
                if (abort_file && abort_file[0] != '\0') {
                    std::ifstream f(abort_file);
                    if (f.good()) {
                        std::cerr << "\n[!] MNA-PCG solver aborted by user flag.\n";
                        return; // Gracefully break the solver loop
                    }
                }
            }
        }
        std::cerr << "[!] MNA-PCG solver reached the iteration limit.\n";
    }

    std::vector<double> getPotential() const {
        return potential;
    }
};

extern "C" {
EXPORT_API
void run_poisson_mna_solver(
    int nx, int ny, int nz,
    const double* x_lines, const double* y_lines, const double* z_lines,
    const double* sigma_map, const double* eps_map, const double* mu_map,
    const double* mask_in, const double* values_in, const double* current_in,
    double* pot_out,
    int max_iterations = 5000,
    double tolerance = 1e-9) {

    try {
        // If you pass a native object pointer or process maps directly, 
        // you can initialize your vectors here:
        std::vector<double> sig(sigma_map, sigma_map + (nx * ny * nz));
        std::vector<double> eps(eps_map, eps_map + (nx * ny * nz));
        std::vector<double> mu(mu_map, mu_map + (nx * ny * nz));

        std::vector<double> x(x_lines, x_lines + nx);
        std::vector<double> y(y_lines, y_lines + ny);
        std::vector<double> z(z_lines, z_lines + nz);

        // Optional: Call the handler step if you pass a native structure pointer
        // extract_csx_materials(native_ptr, nx, ny, nz, x, y, z, sig, eps, mu);

        UniversalMNASolver solver(
            nx, ny, nz,
            x_lines, y_lines, z_lines,
            sig.data(), eps.data(), mu.data(),
            mask_in, values_in, current_in
        );
        
        solver.solve(max_iterations, tolerance);

        const std::vector<double> result = solver.getPotential();
        std::copy(result.begin(), result.end(), pot_out);
    }
    catch (const std::exception& e) {
        std::cerr << "[!] C++ MNA solver error: " << e.what() << "\n";
        const int total = nx * ny * nz;
        for (int i = 0; i < total; ++i) {
            pot_out[i] = std::numeric_limits<double>::quiet_NaN();
        }
    }
}

} // extern "C"