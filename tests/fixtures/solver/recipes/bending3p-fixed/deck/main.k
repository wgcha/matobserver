*KEYWORD
*INCLUDE
mo_params.k
*TITLE
matobserver fixture: 3-point bending (fixed in-house jig)
*INCLUDE
mo_specimen.k
*INCLUDE
mo_mat_body.k
*INCLUDE
mo_controls.k
*INCLUDE
mo_output.k
*INCLUDE
jig.k
*INCLUDE
contact.k
*CONTROL_TERMINATION
   &mo_tend
*PART_MOVE
$#     pid      xmov      ymov      zmov     cdflg
      2001       0.0       0.0  &mo_htop
*BOUNDARY_SPC_SET
       901         0         1         0         0         0         0         0
       902         0         0         1         0         0         0         0
*BOUNDARY_PRESCRIBED_MOTION_RIGID
      2001         3         2       910      -1.0
      2001         1         2       911       0.0
      2001         2         2       911       0.0
      2002         1         2       911       0.0
      2002         2         2       911       0.0
      2002         3         2       911       0.0
*DEFINE_CURVE
       910
                 0.0                 0.0
            &mo_tend            &mo_dend
*DEFINE_CURVE
       911
                 0.0                 0.0
            &mo_tend                 0.0
*END
