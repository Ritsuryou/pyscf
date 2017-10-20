from __future__ import print_function, division
import numpy as np
from scipy.sparse import csr_matrix, coo_matrix
from scipy.linalg import blas
from timeit import default_timer as timer
from pyscf.nao.m_tddft_iter_gpu import tddft_iter_gpu_c
#from pyscf.nao.m_sparse_blas import csrgemv # not working!
from pyscf.nao.m_sparsetools import csr_matvec, csc_matvec, csc_matvecs
import scipy
if int(scipy.__version__[0]) > 0:
    scipy_ver = 1
else:
    scipy_ver = 0
    from pyscf.nao.m_blas_wrapper import spmv_wrapper

try:
    import numba
    from pyscf.nao.m_iter_div_eigenenergy_numba import div_eigenenergy_numba
    use_numba = True
except:
    use_numba = False


class tddft_iter_c():

  def __init__(self, sv, pb, tddft_iter_tol=1e-2, tddft_iter_broadening=0.00367493,
          nfermi_tol=1e-5, telec=None, nelec=None, fermi_energy=None, xc_code='LDA,PZ',
          GPU=False, precision="single", load_kernel=False, **kvargs):
    """ Iterative TDDFT a la PK, DF, OC JCTC """
    from pyscf.nao.m_fermi_dirac import fermi_dirac_occupations
    from pyscf.nao.m_comp_dm import comp_dm

    assert tddft_iter_tol>1e-6
    assert type(tddft_iter_broadening)==float
    assert sv.wfsx.x.shape[-1]==1 # i.e. real eigenvectors we accept here

    if precision == "single":
        self.dtype = np.float32
        self.dtypeComplex = np.complex64
        self.gemm = blas.sgemm
        if scipy_ver > 0:
            self.spmv = blas.sspmv
        else: 
            self.spmv = spmv_wrapper
    elif precision == "double":
        self.dtype = np.float64
        self.dtypeComplex = np.complex128
        self.gemm = blas.dgemm
        if scipy_ver > 0:
            self.spmv = blas.dspmv
        else: 
            self.spmv = spmv_wrapper
    else:
        raise ValueError("precision can be only single or double")

    self.rf0_ncalls = 0
    self.l0_ncalls = 0
    self.matvec_ncalls = 0
    self.tddft_iter_tol = tddft_iter_tol
    self.eps = tddft_iter_broadening
    self.sv, self.pb, self.norbs, self.nspin = sv, pb, sv.norbs, sv.nspin
    self.GPU = GPU

    self.v_dab = pb.get_dp_vertex_sparse(dtype=self.dtype, sparseformat=coo_matrix).tocsr()
    self.cc_da = pb.get_da2cc_sparse(dtype=self.dtype, sparseformat=coo_matrix).tocsr()

    self.moms0,self.moms1 = pb.comp_moments(dtype=self.dtype)
    self.nprod = self.moms0.size

    if load_kernel:
        self.load_kernel(**kvargs)
    else:
        self.kernel,self.kernel_dim = pb.comp_coulomb_pack(dtype=self.dtype) # Lower Triangular Part of the kernel
        assert self.nprod==self.kernel_dim, "%r %r "%(self.nprod, self.kernel_dim)
        
        if xc_code.upper()!='RPA' :
          dm = comp_dm(sv.wfsx.x, sv.get_occupations())
          pb.comp_fxc_pack(dm, xc_code, kernel = self.kernel, dtype=self.dtype, **kvargs)

    self.telec = sv.hsx.telec if telec is None else telec
    self.nelec = sv.hsx.nelec if nelec is None else nelec
    self.fermi_energy = sv.fermi_energy if fermi_energy is None else fermi_energy

    # probably unnecessary, require probably does a copy
    # problematic for the dtype, must there should be another option 
    #self.x  = np.require(sv.wfsx.x, dtype=self.dtype, requirements='CW')

    self.ksn2e = np.require(sv.wfsx.ksn2e, dtype=self.dtype, requirements='CW')
    ksn2fd = fermi_dirac_occupations(self.telec, self.ksn2e, self.fermi_energy)
    self.ksn2f = (3-self.nspin)*ksn2fd
    self.nfermi = np.argmax(ksn2fd[0,0,:]<nfermi_tol)
    self.vstart = np.argmax(1.0-ksn2fd[0,0,:]>nfermi_tol)

    self.xocc = sv.wfsx.x[0,0,0:self.nfermi,:,0]  # does python creates a copy at this point ?
    self.xvrt = sv.wfsx.x[0,0,self.vstart:,:,0]   # does python creates a copy at this point ?

    self.tddft_iter_gpu = tddft_iter_gpu_c(GPU, sv.wfsx.x, self.v_dab, self.ksn2f, self.ksn2e, 
            self.cc_da, self.norbs, self.nfermi, self.nprod, self.vstart)

  def load_kernel(self, kernel_fname, kernel_format="npy", kernel_path_hdf5=None, **kwargs):

      if kernel_format == "npy":
          self.kernel = np.load(kernel_fname)
      elif kernel_format == "txt":
          self.kernel = np.float32(np.loadtxt(kernel_fname))
      elif kernel_format == "hdf5":
          import h5py
          if kernel_path_hdf5 is None:
              raise ValueError("kernel_path_hdf5 not set while trying to read kernel from hdf5 file.")
          self.kernel = h5py.File(kernel_fname, "r")[kernel_path_hdf5].value
      else:
          raise ValueError("Wrong format for loading kernel, must be: npy, txt or hdf5, got " + kernel_format)

      if len(self.kernel.shape) > 1:
          raise ValueError("The kernel must be saved in packed format in order to be loaded!")
      
      assert self.nprod*(self.nprod+1)//2 == self.kernel.size, "wrong size for loaded kernel: %r %r "%(self.nprod*(self.nprod+1)//2, self.kernel.size)
      self.kernel_dim = self.nprod

  def apply_rf0(self, v, comega=1j*0.0):
    """ This applies the non-interacting response function to a vector (a set of vectors?) """
    assert len(v)==len(self.moms0), "%r, %r "%(len(v), len(self.moms0))
    self.rf0_ncalls+=1
    no = self.norbs
    #print("vKs = ", np.sum(abs(v)))

    if v.dtype == self.dtypeComplex:
        vext = np.zeros((v.shape[0], 2), dtype = self.dtype, order="F")
        vext[:, 0] = v.real
        vext[:, 1] = v.imag

        # real part
        #vdp = self.cc_da*vext[:, 0]
        vdp = csr_matvec(self.cc_da, vext[:, 0])
        sab = (vdp*self.v_dab).reshape([no,no])
        nb2v = self.gemm(1.0, self.xocc, sab) 
        #csc_matvecs(sab.T.tocsc(), self.xocc, transB = True).T
        nm2v_re = self.gemm(1.0, nb2v, np.transpose(self.xvrt))
        
        # imaginary part
        vdp = csr_matvec(self.cc_da, vext[:, 1])
        sab = (vdp*self.v_dab).reshape([no,no])
        nb2v = self.gemm(1.0, self.xocc, sab) 
        nm2v_im = self.gemm(1.0, nb2v, np.transpose(self.xvrt))
    else: # it gets mistaken here when double-precision kernel is accidentally used  
        vext = np.zeros((v.shape[0], 2), dtype = self.dtype, order="F")
        vext[:, 0] = v

        # real part
        #vdp = self.cc_da*vext[:, 0]
        vdp = csr_matvec(self.cc_da, vext[:, 0])
        sab = (vdp*self.v_dab).reshape([no,no])
        nb2v = self.gemm(1.0, self.xocc, sab) 
        nm2v_re = self.gemm(1.0, nb2v, np.transpose(self.xvrt))
 
        # imaginary part
        nm2v_im = np.zeros(nm2v_re.shape, dtype=self.dtype) 
   
    #vdp = csrgemv(self.cc_da, vext) # np.require(v, dtype=np.complex64)

    if use_numba:
        div_eigenenergy_numba(self.ksn2e, self.ksn2f, self.nfermi, self.vstart, comega, nm2v_re, nm2v_im, self.ksn2e.shape[2])
    else:
        for n,[en,fn] in enumerate(zip(self.ksn2e[0,0,:self.nfermi],self.ksn2f[0,0,:self.nfermi])):
          for j,[em,fm] in enumerate(zip(self.ksn2e[0,0,n+1:],self.ksn2f[0,0,n+1:])):
            m = j+n+1-self.vstart
            nm2v = nm2v_re[n, m] + 1.0j*nm2v_im[n, m]
            nm2v = nm2v * (fn-fm) *\
              ( 1.0 / (comega - (em - en)) - 1.0 / (comega + (em - en)) )
            nm2v_re[n, m] = nm2v.real
            nm2v_im[n, m] = nm2v.imag

    nb2v = self.gemm(1.0, nm2v_re, self.xvrt)
    ab2v = self.gemm(1.0, self.xocc.T, nb2v).reshape(no*no)
    vdp = csr_matvec(self.v_dab, ab2v)

    chi0_re = vdp*self.cc_da

    nb2v = self.gemm(1.0, nm2v_im, self.xvrt)
    ab2v = self.gemm(1.0, self.xocc.T, nb2v).reshape(no*no)
    vdp = csr_matvec(self.v_dab, ab2v)

    chi0_im = vdp*self.cc_da
    #chi0_im = self.cc_ad_csc*vdp
    #print("chi0 = ", np.sum(abs(chi0_re)), np.sum(abs(chi0_im)))
    #import sys
    #sys.exit()

    return chi0_re + 1.0j*chi0_im


  def comp_veff(self, vext, comega=1j*0.0, x0=None):
    #from scipy.sparse.linalg import gmres, lgmres as gmres_alias, LinearOperator
    from scipy.sparse.linalg import lgmres, LinearOperator
    
    """ This computes an effective field (scalar potential) given the external scalar potential """
    assert len(vext)==len(self.moms0), "%r, %r "%(len(vext), len(self.moms0))
    self.comega_current = comega
    veff_op = LinearOperator((self.nprod,self.nprod), matvec=self.vext2veff_matvec, dtype=self.dtypeComplex)
    resgm = lgmres(veff_op, np.require(vext, dtype=self.dtypeComplex, 
        requirements='C'), x0=x0, tol=self.tddft_iter_tol)
    return resgm
  
  def vext2veff_matvec(self, v):
    self.matvec_ncalls+=1 
    
    if self.GPU:
        self.tddft_iter_gpu.apply_rf0_gpu(v, self.comega_current)
    else:
        chi0 = self.apply_rf0(v, self.comega_current)
    
    # For some reason it is very difficult to pass only one dimension
    # of an array to the fortran routines?? matvec[0, :].ctypes.data_as(POINTER(c_float))
    # is not working!!!

    # real part
    chi0_reim = np.require(chi0.real, dtype=self.dtype, requirements=["A", "O"])
    matvec_real = self.spmv(self.nprod, 1.0, self.kernel, chi0_reim, lower=1)
    
    # imaginary part
    chi0_reim = np.require(chi0.imag, dtype=self.dtype, requirements=["A", "O"])
    matvec_imag = self.spmv(self.nprod, 1.0, self.kernel, chi0_reim, lower=1)

    return v - (matvec_real + 1.0j*matvec_imag)

  def comp_polariz_xx(self, comegas, x0=False):
    """ 
        Compute interacting polarizability

        Inputs:
        -------
            comegas (complex 1D array): frequency range (in Hartree) for which the polarizability is computed.
                                     The imaginary part control the width of the signal.
                                     For example, 
                                     td = tddft_iter_c(...)
                                     comegas = np.arange(0.0, 10.05, 0.05) + 1j*td.eps
            x0 (boolean, optional): determine if a starting guess array should be use to
                                    guess the solution. if True, it will use the non-interacting 
                                    polarizability as guess.
        Output:
        -------
            polariz (complex 1D array): computed polarizability
            self.dn (complex 2D array): computed density change in prod basis
        
    """
    polariz = np.zeros_like(comegas, dtype=np.complex64)
    self.dn = np.zeros((comegas.shape[0], self.nprod), dtype=np.complex64)
    
    for iw,comega in enumerate(comegas):
        if x0 == True:
            veff,info = self.comp_veff(self.moms1[:,0], comega, x0=self.dn0[iw, :])
        else:
            veff,info = self.comp_veff(self.moms1[:,0], comega, x0=None)

        if self.GPU:
            self.dn[iw, :] = self.tddft_iter_gpu.apply_rf0_gpu(veff, comega)
        else:
            self.dn[iw, :] = self.apply_rf0(veff, comega)
     
        polariz[iw] = np.dot(self.moms1[:,0], self.dn[iw, :])

    if self.tddft_iter_gpu.GPU:
        self.tddft_iter_gpu.clean_gpu()

    return polariz

  def comp_nonin(self, comegas):
    """ 
        Compute non-interacting polarizability

        Inputs:
        -------
            comegas (complex 1D array): frequency range (in Hartree) for which the polarizability is computed.
                                     The imaginary part control the width of the signal.
                                     For example, 
                                     td = tddft_iter_c(...)
                                     comegas = np.arange(0.0, 10.05, 0.05) + 1j*td.eps
        Output:
        -------
            pxx (complex 1D array): computed non-interacting polarizability
            self.dn0 (complex 2D array): computed non-interacting density change in prod basis
        
    """

    vext = np.transpose(self.moms1)
    pxx = np.zeros(comegas.shape, dtype=np.complex64)
    self.dn0 = np.zeros((comegas.shape[0], self.nprod), dtype=np.complex64)

    for iw, omega in enumerate(comegas):
        if self.GPU:
            self.dn0[iw, :] = -self.tddft_iter_gpu.apply_rf0_gpu(vext[0, :], omega)
        else:
            self.dn0[iw, :] = -self.apply_rf0(vext[0, :], omega)
 
        pxx[iw] = np.dot(self.dn0[iw, :], vext[0,:])
    return pxx
