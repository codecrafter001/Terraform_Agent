# State migration: move previously applied resource addresses to adopted module names
moved {
  from = aws_instance.ubuntu_server
  to   = aws_instance.terraagent_ubuntu_ec2_0ae26311
}

moved {
  from = aws_security_group.ec2_sg
  to   = aws_security_group.terraagent_ubuntu_ec2_sg_56255343
}

moved {
  from = aws_subnet.public_subnet
  to   = aws_subnet.terraagent_ubuntu_ec2_subnet_4a2dbe19
}
