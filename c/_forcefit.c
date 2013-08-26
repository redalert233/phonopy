#include <Python.h>
#include <numpy/arrayobject.h>
#include "lapack_wrapper.h"

static PyObject * py_phonopy_pinv(PyObject *self, PyObject *args);
static PyObject * py_phonopy_pinv_mt(PyObject *self, PyObject *args);
static PyObject * py_displacement_matrix_fc4(PyObject *self, PyObject *args);
void get_tensor1(double sym_u[9], const double *u, const double *sym);
int set_tensor2(double *disp_matrix, const double u[9]);
int set_tensor3(double *disp_matrix, const double u[9]);

static PyMethodDef functions[] = {
  {"pinv", py_phonopy_pinv, METH_VARARGS, "Pseudo-inverse using Lapack dgesvd"},
  {"pinv_mt", py_phonopy_pinv_mt, METH_VARARGS, "Multi-threading pseudo-inverse using Lapack dgesvd"},
  {"displacement_matrix_fc4", py_displacement_matrix_fc4, METH_VARARGS, "Create displacement matrix for fc4"},
  {NULL, NULL, 0, NULL}
};

PyMODINIT_FUNC init_forcefit(void)
{
  Py_InitModule3("_forcefit", functions, "C-extension for force-fit\n\n...\n");
  return;
}


static PyObject * py_phonopy_pinv(PyObject *self, PyObject *args)
{
  PyArrayObject* data_in_py;
  PyArrayObject* data_out_py;
  double cutoff;

  if (!PyArg_ParseTuple(args, "OOd",
			&data_in_py,
			&data_out_py,
			&cutoff)) {
    return NULL;
  }

  const int m = (int)data_in_py->dimensions[0];
  const int n = (int)data_in_py->dimensions[1];
  const double *data_in = (double*)data_in_py->data;
  double *data_out = (double*)data_out_py->data;
  int info;
  
  info = phonopy_pinv(data_out, data_in, m, n, cutoff);

  return PyInt_FromLong((long) info);
}

static PyObject * py_phonopy_pinv_mt(PyObject *self, PyObject *args)
{
  PyArrayObject* data_in_py;
  PyArrayObject* data_out_py;
  PyArrayObject* row_nums_py;
  PyArrayObject* info_py;
  int max_row_num, column_num;
  double cutoff;

  if (!PyArg_ParseTuple(args, "OOOiidO",
			&data_in_py,
			&data_out_py,
			&row_nums_py,
			&max_row_num,
			&column_num,
			&cutoff,
			&info_py)) {
    return NULL;
  }

  const int *row_nums = (int*)row_nums_py->data;
  const int num_thread = (int)row_nums_py->dimensions[0];
  const double *data_in = (double*)data_in_py->data;
  double *data_out = (double*)data_out_py->data;
  int *info = (int*)info_py->data;
  
  phonopy_pinv_mt(data_out,
		  info,
		  data_in,
		  num_thread,
		  row_nums,
		  max_row_num,
		  column_num,
		  cutoff);

  Py_RETURN_NONE;
}

static PyObject * py_displacement_matrix_fc4(PyObject *self, PyObject *args)
{
  PyArrayObject* disp_matrix_py;
  PyArrayObject* disp_triplets_py;
  PyArrayObject* num_disps_py;
  PyArrayObject* site_syms_cart_py;
  PyArrayObject* rot_map_syms_py;
  int second_atom_num, third_atom_num;

  if (!PyArg_ParseTuple(args, "OiiOOOO",
			&disp_matrix_py,
			&second_atom_num,
			&third_atom_num,
			&disp_triplets_py,
			&num_disps_py,
			&site_syms_cart_py,
			&rot_map_syms_py)) {
    return NULL;
  }

  double *disp_matrix = (double*)disp_matrix_py->data;
  const int *num_disps = (int*)num_disps_py->data;
  const int num_first_disps = (int)num_disps_py->dimensions[0];
  const int num_atom = (int)num_disps_py->dimensions[1];
  const double *disp_triplets = (double*)disp_triplets_py->data;
  const double *site_syms_cart = (double*)site_syms_cart_py->data;
  const int num_site_syms = (int)site_syms_cart_py->dimensions[0];
  const int *rot_map_syms = (int*)rot_map_syms_py->data;

  int i, j, k, l, rot_num2, rot_num3, address, num_disp, count;
  double sym_u[9];

  count = 0;
  for (i = 0; i < num_first_disps; i++) {
    for (j = 0; j < num_site_syms; j++) {
      rot_num2 = rot_map_syms[num_atom * j + second_atom_num];
      rot_num3 = rot_map_syms[num_atom * j + third_atom_num];
      address = num_disps[i * 2 * num_atom * num_atom +
			  2 * rot_num2 * num_atom +
			  2 * rot_num3];
      num_disp = num_disps[i * 2 * num_atom * num_atom +
			   2 * rot_num2 * num_atom +
			   2 * rot_num3 + 1];
      for (k = 0; k < num_disp; k++) {
	disp_matrix[count] = -1;
	count++;
	get_tensor1(sym_u,
		    disp_triplets + (address + k) * 9,
		    site_syms_cart + j * 9);
	for (l = 0; l < 9; l++) {
	  disp_matrix[count] = sym_u[l];
	  count++;
	}
	count += set_tensor2(disp_matrix + count, sym_u);
	count += set_tensor3(disp_matrix + count, sym_u);
      }
    }
  }
  return PyInt_FromLong((long) count);
}

void get_tensor1(double sym_u[9], const double *u, const double *sym)
{
  int i, j, k;

  for (i = 0; i < 3; i++) {
    for (j = 0; j < 3; j++) {
      sym_u[i * 3 + j] = 0;
      for (k = 0; k < 3; k++) {
	sym_u[i * 3 + j] += sym[j * 3 + k] * u[i * 3 + k];
      }
    }
  }
}

int set_tensor2(double *disp_matrix, const double u[9])
{
  int i, j, k, p1, p2, count;
  static int pairs[6][3] = {{0, 0},
			    {0, 1},
			    {0, 2},
			    {1, 1},
			    {1, 2},
			    {2, 2}};

  count = 0;
  for (i = 0; i < 6; i++) {
    p1 = pairs[i][0];
    p2 = pairs[i][1];
    for (j = 0; j < 3; j++) {
      for (k = 0; k < 3; k++) {
	disp_matrix[count] = u[p1 * 3 + j] * u[p2 * 3 + k];
	count++;
      }
    }
  }
  return count;
}

int set_tensor3(double *disp_matrix, const double u[9])
{
  int i, j, k, l, t1, t2, t3, count;
  static int triplets[10][3] = {{0, 0, 0},
				{0, 0, 1},
				{0, 0, 2},
				{0, 1, 1},
				{0, 1, 2},
				{0, 2, 2},
				{1, 1, 1},
				{1, 1, 2},
				{1, 2, 2},
				{2, 2, 2}};

  count = 0;
  for (i = 0; i < 10; i++) {
    t1 = triplets[i][0];
    t2 = triplets[i][1];
    t3 = triplets[i][2];
    for (j = 0; j < 3; j++) {
      for (k = 0; k < 3; k++) {
	for (l = 0; l < 3; l++) {
	  disp_matrix[count] = u[t1 * 3 + j] * u[t2 * 3 + k] * u[t3 * 3 + l];
	  count++;
	}
      }
    }
  }
  return count;
}
