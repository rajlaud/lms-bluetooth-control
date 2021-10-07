scp -q lms-bluetooth-control/* pi@media-pi.local:/srv/lms-bluetooth-control
ssh pi@media-pi.local sudo systemctl restart lms-bluetooth-control