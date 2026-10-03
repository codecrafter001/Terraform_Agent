data "aws_security_group" "default_937736ee" {
  id = "sg-05c1ea353a9ac2360"
}

resource "aws_security_group" "terraagent_ubuntu_ec2_sg_56255343" {
  name        = "terraagent-ubuntu-ec2-sg"
  description = "Security group for TerraAgent Ubuntu EC2 instance"
  vpc_id      = data.aws_vpc.vpc_041d35e78174e8bf4_75fd5433.id

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "HTTP web traffic"
  }

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "SSH from authorized CIDRs"
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
    description = "Allow all outbound traffic"
  }

  tags = {
    "Name" = "terraagent-ubuntu-ec2-sg"
  }
}