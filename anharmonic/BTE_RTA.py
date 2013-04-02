import numpy as np
import phonopy.structure.spglib as spg
from anharmonic.im_self_energy import get_gamma
from phonopy.group_velocity import get_group_velocity
from phonopy.units import Kb, THzToEv, EV, THz, Angstrom
from anharmonic.file_IO import parse_kappa, write_kappa, write_gamma_to_hdf5
from anharmonic.triplets import get_grid_address, reduce_grid_points

unit_to_WmK = ((THz * Angstrom) ** 2 / (Angstrom ** 3) * EV / THz /
               (2 * np.pi)) # 2pi comes from definition of lifetime.

class BTE_RTA:
    def __init__(self,
                 interaction_strength,
                 sigmas=[0.1],
                 t_max=1500,
                 t_min=0,
                 t_step=10,
                 max_freepath=0.01, # in meter
                 mesh_divisors=None,
                 no_kappa_stars=False,
                 gamma_option=0,
                 log_level=0,
                 write_logs=True,
                 filename=None):
        self._pp = interaction_strength
        self._sigmas = sigmas
        self._t_max = t_max
        self._t_min = t_min
        self._t_step = t_step
        self._max_freepath = max_freepath
        self._no_kappa_stars = no_kappa_stars
        self._gamma_option = gamma_option
        self._log_level = log_level
        self._write_logs = write_logs
        self._filename = filename

        self._temperatures = np.arange(self._t_min,
                                       self._t_max + float(self._t_step) / 2,
                                       self._t_step)
        self._primitive = self._pp.get_primitive()
        self._cutoff_frequency = self._pp.get_cutoff_frequency()
        self._freq_conv_factor = self._pp.get_frequency_unit_conversion_factor()
        self._reciprocal_lattice = np.linalg.inv(
            self._primitive.get_cell()) # a*, b*, c* are column vectors.
        self._grid_points = None
        self._grid_weights = None
        self._grid_address = None

        self._point_operations = None
        self._set_pointgroup_operations()
        self._gamma = None

        self._mesh = None
        self._mesh_divisors = None
        self._set_mesh_numbers(mesh_divisors=mesh_divisors)
        volume = self._primitive.get_volume()
        num_grid = np.prod(self._mesh / self._mesh_divisors)
        self._conversion_factor = unit_to_WmK / volume / num_grid

    def get_mesh_divisors(self):
        return self._mesh_divisors

    def get_mesh_numbers(self):
        return self._mesh
        
    def set_grid_points(self, grid_points=None):
        if grid_points is not None: # Specify grid points
            self._grid_address = get_grid_address(self._mesh)
            dense_grid_points = grid_points
            self._grid_points = reduce_grid_points(self._mesh_divisors,
                                                   self._grid_address,
                                                   dense_grid_points)
        elif self._no_kappa_stars: # All grid points
            self._grid_address = get_grid_address(self._mesh)
            dense_grid_points = range(np.prod(self._mesh))
            self._grid_points = reduce_grid_points(self._mesh_divisors,
                                                   self._grid_address,
                                                   dense_grid_points)
        else: # Automatic sampling
            (grid_mapping_table,
             self._grid_address) = spg.get_ir_reciprocal_mesh(self._mesh,
                                                              self._primitive)
            dense_grid_points = np.unique(grid_mapping_table)
            dense_grid_weights = [np.sum(grid_mapping_table == g)
                                  for g in dense_grid_points]

            self._grid_points, self._grid_weights = reduce_grid_points(
                self._mesh_divisors,
                self._grid_address,
                dense_grid_points,
                dense_grid_weights)

            assert self._grid_weights.sum() == np.prod(self._mesh /
                                                       self._mesh_divisors)

    def get_grid_address(self):
        return self._grid_points
            
    def set_temperatures(self, temperatures):
        self._temperatures = temperatures

    def get_temperatures(self):
        return self._temperatures

    def set_gamma(self, gamma):
        self._gamma = gamma
    
    def get_kappa(self):
        num_atom = self._primitive.get_number_of_atoms()
        kappa = np.zeros((len(self._sigmas),
                          len(self._grid_points),
                          len(self._temperatures),
                          num_atom * 3), dtype=float)
        
        if self._gamma is None: # if gamma is not set.
            gamma = np.zeros_like(kappa)
        else:
            gamma = self._gamma

        for i in range(len(self._grid_points)):
            grid_point = self._grid_points[i]
            if self._log_level:
                print ("================= %d/%d =================" %
                       (i + 1, len(self._grid_points)))

            if self._gamma is None:
                self._pp.set_triplets_at_q(grid_point)
                self._pp.set_interaction_strength()
                self._pp.set_harmonic_phonons()
            else:
                self._pp.set_qpoint(
                    self._grid_address[grid_point].astype(float) / self._mesh)
                self._pp.set_harmonic_phonons()

            self._get_kappa_at_sigmas(i, kappa, gamma)
            
            if self._write_logs:
                for j, sigma in enumerate(self._sigmas):
                    write_kappa(kappa[j, i].sum(axis=1),
                                self._temperatures,
                                self._mesh,
                                mesh_divisors=self._mesh_divisors,
                                gamma=gamma[j, i],
                                grid_point=grid_point,
                                sigma=sigma,
                                filename=self._filename)

                    write_gamma_to_hdf5(gamma[j, i],
                                        kappa[j, i],
                                        self._temperatures,
                                        self._pp.get_frequencies(),
                                        self._mesh,
                                        mesh_divisors=self._mesh_divisors,
                                        grid_point=grid_point,
                                        sigma=sigma,
                                        filename=self._filename)

        if self._write_logs:
            if self._grid_weights is not None:
                print "-------------- Total kappa --------------"
                for sigma, kappa_at_sigma in zip(self._sigmas, kappa):
                    write_kappa(kappa_at_sigma.sum(axis=0).sum(axis=1),
                                self._temperatures,
                                self._mesh,
                                mesh_divisors=self._mesh_divisors,
                                sigma=sigma,
                                filename=self._filename)

        return kappa, gamma

    def _get_kappa_at_sigmas(self,
                             i,
                             kappa,
                             gamma):
        # Group velocity
        gv2_tensor = self._get_gv_by_gv(i)
        gv_sum2 = gv2_tensor[:, :, 0, 0].sum(axis=0) # currently only [100] direction
        # Heat capacity
        cv = self._get_cv()

        for j, sigma in enumerate(self._sigmas):
            if self._log_level > 0:
                print "Sigma used to approximate delta function by gaussian: %f" % sigma

            if self._gamma is None:
                gamma[j, i] = self._get_gamma(sigma)
            for k in range(len(self._temperatures)):
                for l in range(len(self._pp.get_frequencies())):
                    gv = max(np.sqrt(gv2_tensor[:, l, 0, 0]))
                    if gamma[j, i, k, l] > 0:
                        kappa[j, i, k, l] = (gv_sum2[l] * cv[k, l] /
                                             gamma[j, i, k, l] / 2 *
                                             self._conversion_factor)

    def _get_gv_by_gv(self, index):
        grid_point = self._grid_points[index]
        
        # Group velocity
        gv = get_group_velocity(
            self._pp.get_qpoint(),
            self._pp.get_dynamical_matrix(),
            self._reciprocal_lattice,
            eigenvectors=self._pp.get_eigenvectors(),
            frequencies=self._pp.get_frequencies())

        # Sum group velocities at symmetrically equivalent q-points
        if self._no_kappa_stars:
            rot_unit_n = [np.eye(3, dtype=float)]
        else:
            rot_unit_n = self._get_rotated_unit_directions(grid_point)
            # check if the number of rotations is correct.
            if self._grid_weights is not None:
                assert len(rot_unit_n) == self._grid_weights[index], \
                    "Num rot_unit_n %d, weight %d" % (
                    len(rot_unit_n), self._grid_weights[index])
            
        gv2_tensor = []
        for unit_n in rot_unit_n:
            gv2_tensor.append([np.outer(gv_xyz, gv_xyz)
                               for gv_xyz in np.dot(gv, unit_n)])

        self._show_log(grid_point, gv, rot_unit_n)

        return np.array(gv2_tensor)
    
    def _get_cv(self):
        def get_cv(freqs, t):
            x = freqs * THzToEv / Kb / t
            expVal = np.exp(x)
            return Kb * x ** 2 * expVal / (expVal - 1.0) ** 2 # eV/K

        freqs = self._pp.get_frequencies()
        cv = np.zeros((len(self._temperatures), len(freqs)), dtype=float)
        for i, t in enumerate(self._temperatures):
            if t > 0:
                for j, f in enumerate(freqs):
                    if f > self._cutoff_frequency:
                        cv[i, j] = get_cv(f / self._freq_conv_factor, t)

        return cv

    def _get_gamma(self, sigma):
        unit_conversion = self._pp.get_unit_conversion_factor()
        freqs = self._pp.get_frequencies()

        (amplitude_at_q,
         weights_at_q,
         frequencies_at_q) = self._pp.get_amplitude()

        gamma = -1 * np.ones((len(self._temperatures), len(freqs)), dtype=float)

        for i, t in enumerate(self._temperatures):
            if t > 0:
                for j, f in enumerate(freqs):
                    if f > self._cutoff_frequency:
                        g = get_gamma(
                            amplitude_at_q,
                            np.array([f], dtype=float),
                            weights_at_q,
                            frequencies_at_q,
                            j,
                            t,
                            sigma,
                            self._freq_conv_factor,
                            cutoff_frequency=self._cutoff_frequency,
                            gamma_option=self._gamma_option
                            )[0] * unit_conversion # THz in default
                        gamma[i, j] = g
        return gamma

    def _get_rotated_unit_directions(self, grid_point):
        rec_lat = np.linalg.inv(self._primitive.get_cell())
        inv_rec_lat = self._primitive.get_cell()
        orig_address = self._grid_address[grid_point]
        orbits = []
        rot_unit_n = []
        for rot in self._point_operations:
            rot_address = np.dot(rot, orig_address) % self._mesh
            in_orbits = False
            for orbit in orbits:
                if (rot_address == orbit).all():
                    in_orbits = True
                    break
            if not in_orbits:
                orbits.append(rot_address)
                rot_cart = np.dot(rec_lat, np.dot(rot, inv_rec_lat))
                rot_unit_n.append(rot_cart.T)
                # Column vectors of rot_cart.T correspond to rotated unit
                # vectors of [1, 0, 0], [0, 1, 0], [0, 0, 1], respectively.

        return rot_unit_n

    def _set_mesh_numbers(self, mesh=None, mesh_divisors=None):
        if mesh is None:
            self._mesh = self._pp.get_mesh_numbers()
        else:
            self._mesh = np.array(mesh, dtype=int)

        if mesh_divisors is None:
            self._mesh_divisors = np.array([1, 1, 1], dtype=int)
        else:
            self._mesh_divisors = []
            for m, n in zip(self._mesh, mesh_divisors):
                if m % n == 0:
                    self._mesh_divisors.append(n)
                else:
                    self._mesh_divisors.append(1)
            self._mesh_divisors = np.array(self._mesh_divisors, dtype=int)

            if (self._mesh_divisors != mesh_divisors).any():
                print "Mesh numbers are not dividable by mesh divisors."
        if self._log_level:
            print ("Lifetime sampling mesh: [ %d %d %d ]" %
                   tuple(self._mesh / self._mesh_divisors))

    def _sort_normal_umklapp(self):
        normal_a = []
        normal_w = []
        normal_f = []
        umklapp_a = []
        umklapp_w = []
        umklapp_f = []
        (amplitude_at_q,
         weights_at_q,
         frequencies_at_q) = self._pp.get_amplitude()
        triplets_at_q = self._pp.get_triplets_at_q()
        for a, w, f, g3 in zip(amplitude_at_q,
                               weights_at_q,
                               frequencies_at_q,
                               triplets_at_q):
            sum_q = (self._grid_address[g3[0]] +
                     self._grid_address[g3[1]] +
                     self._grid_address[g3[2]])
            if (sum_q == 0).all():
                normal_a.append(a)
                normal_w.append(w)
                normal_f.append(f)
                print "N",
            else:
                umklapp_a.append(a)
                umklapp_w.append(w)
                umklapp_f.append(f)
                print "U",

        print
        return ((np.array(normal_a, dtype=float),
                 np.array(normal_w, dtype=int),
                 np.array(normal_f, dtype=float)),
                (np.array(umklapp_a, dtype=float),
                 np.array(umklapp_w, dtype=int),
                 np.array(umklapp_f, dtype=float)))
    
    def _set_pointgroup_operations(self):
        exist_r_inv = False
        for rot in self._pp.get_symmetry().get_pointgroup_operations():
            if (rot == -np.eye(3, dtype=int)).all():
                exist_r_inv = True
                break

        point_operations = [
            rot.T for rot in
            self._pp.get_symmetry().get_pointgroup_operations()]
        
        if not exist_r_inv:
            point_operations += [
                -rot.T for rot in
                 self._pp.get_symmetry().get_pointgroup_operations()]
            
        self._point_operations = np.array(point_operations)

    def _show_log(self, grid_point, group_velocity, rot_unit_n):
        if self._log_level:
            print "----- Partial kappa at grid address %d -----" % grid_point
            print "Frequency, Group velocity (x y z):"
            for f, v in zip(self._pp.get_frequencies(), group_velocity):
                print "%8.3f (%8.3f %8.3f %8.3f)" % ((f,) + tuple(v))
            print "Frequency, projected group velocity (GV), and GV squared"
            for unit_n in rot_unit_n:
                print "Directions [%4.1f %4.1f %4.1f], [%4.1f %4.1f %4.1f], [%4.1f %4.1f %4.1f]" % tuple(unit_n.flatten())
                for f, v in zip(self._pp.get_frequencies(),
                                np.dot(group_velocity, unit_n)):
                    print "%8.3f (%8.3f %8.3f %8.3f)" % ((f,) + tuple(v))

        
            
        
if __name__ == '__main__':
    import sys
    import h5py

    def sum_partial_kappa(filenames):
        temps, kappa = parse_kappa(filenames[0])
        sum_kappa = np.array(kappa)
        for filename in filenames[1:]:
            temps, kappa = parse_kappa(filename)
            sum_kappa += kappa
        return temps, sum_kappa
    
    def sum_partial_kappa_hdf5(filenames):
        f = h5py.File(filenames[0], 'r')
        kappas = f['kappas'][:]
        temps = f['temperatures'][:]
        for filename in filenames[1:]:
            f = h5py.File(filename, 'r')
            kappas += f['kappas'][:]
        return temps, kappas

    # temps, kappa = sum_partial_kappa(sys.argv[1:])
    # for t, k in zip(temps, kappa):
    #     print "%8.2f %.5f" % (t, k)
    temps, kappa = sum_partial_kappa_hdf5(sys.argv[1:])
    for t, k in zip(temps, kappa.sum(axis=1)):
        print "%8.2f %.5f" % (t, k)

