resource "aws_instance" "terraagent_ubuntu_ec2_0ae26311" {
  ami                    = "ami-062944a84867f2386"
  instance_type          = "t3.micro"
  subnet_id              = aws_subnet.terraagent_ubuntu_ec2_subnet_4a2dbe19.id
  vpc_security_group_ids = [aws_security_group.terraagent_ubuntu_ec2_sg_56255343.id]

  tags = {
    "Name" = "terraagent-ubuntu-ec2"
  }
}