data "aws_vpc" "vpc_041d35e78174e8bf4_75fd5433" {
  id = "vpc-041d35e78174e8bf4"
}

resource "aws_subnet" "terraagent_ubuntu_ec2_subnet_4a2dbe19" {
  vpc_id                  = data.aws_vpc.vpc_041d35e78174e8bf4_75fd5433.id
  cidr_block              = "172.31.1.0/24"
  availability_zone       = "us-east-1a"
  map_public_ip_on_launch = true

  tags = {
    "Name" = "terraagent-ubuntu-ec2-subnet"
  }
}

resource "aws_route_table" "rtb_003b648347930edfc_24811887" {
  vpc_id = data.aws_vpc.vpc_041d35e78174e8bf4_75fd5433.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = "igw-069cc92d846c417f0"
  }
}

data "aws_internet_gateway" "igw_069cc92d846c417f0_f19bc3c9" {
  internet_gateway_id = "igw-069cc92d846c417f0"
}