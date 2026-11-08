*KEYWORD
*INCLUDE
mo_params.k
*TITLE
matobserver fixture: 3-point bending (parametric rollers, quarter symmetry)
$ ---------------------------------------------------------------- engine includes
*INCLUDE
mo_specimen.k
*INCLUDE
mo_mat_body.k
*INCLUDE
mo_jig.k
*INCLUDE
mo_controls.k
*INCLUDE
mo_output.k
*INCLUDE
contact.k
$ ---------------------------------------------------------------- termination
*CONTROL_TERMINATION
$#  endtim    endcyc     dtmin    endeng    endmas
   &mo_tend         0       0.0       0.0       0.0
$ ---------------------------------------------------------------- symmetry planes
*BOUNDARY_SPC_SET
$#    nsid       cid      dofx      dofy      dofz     dofrx     dofry     dofrz
       901         0         1         0         0         0         0         0
       902         0         0         1         0         0         0         0
$ ---------------------------------------------------------------- indenter: -z displacement, others fixed
*BOUNDARY_PRESCRIBED_MOTION_RIGID
$#     pid       dof       vad      lcid        sf       vid     death     birth
      1001         3         2       910      -1.0
      1001         1         2       911       0.0
      1001         2         2       911       0.0
      1001         5         2       911       0.0
      1001         6         2       911       0.0
      1001         7         2       911       0.0
      1002         1         2       911       0.0
      1002         2         2       911       0.0
      1002         3         2       911       0.0
      1002         5         2       911       0.0
      1002         6         2       911       0.0
      1002         7         2       911       0.0
*DEFINE_CURVE_TITLE
indenter stroke: 0 -> mo_dend over mo_tend
$#    lcid      sidr       sfa       sfo      offa      offo    dattyp
       910         0       1.0       1.0       0.0       0.0         0
                 0.0                 0.0
            &mo_tend            &mo_dend
*DEFINE_CURVE_TITLE
zero
       911         0       1.0       1.0       0.0       0.0         0
                 0.0                 0.0
            &mo_tend                 0.0
*END
