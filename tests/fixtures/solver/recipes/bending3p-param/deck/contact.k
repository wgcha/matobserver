*KEYWORD
$ contact 1: indenter (master, part 1001) - specimen (slave, part set 900)
*CONTACT_AUTOMATIC_SURFACE_TO_SURFACE_ID
$#     cid                                                                 heading
         1indenter
$#    ssid      msid     sstyp     mstyp    sboxid    mboxid       spr       mpr
       900      1001         2         3         0         0         1         1
$#      fs        fd        dc        vc       vdc    penchk        bt        dt
     &fric     &fric       0.0       0.0      20.0         0       0.0    1.0E20
$#     sfs       sfm       sst       mst      sfst      sfmt       fsf       vsf
       1.0       1.0       0.0       0.0       1.0       1.0       1.0       1.0
$ contact 2: support roller (master, part 1002) - specimen
*CONTACT_AUTOMATIC_SURFACE_TO_SURFACE_ID
         2support
       900      1002         2         3         0         0         1         1
     &fric     &fric       0.0       0.0      20.0         0       0.0    1.0E20
       1.0       1.0       0.0       0.0       1.0       1.0       1.0       1.0
*END
