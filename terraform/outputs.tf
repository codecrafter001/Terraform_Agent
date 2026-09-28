output "instance_id" {
  description = "ID of the deployed Ubuntu EC2 instance"
  value       = aws_instance.ubuntu_server.id
}

output "instance_public_ip" {
  description = "Public IP address of the EC2 instance"
  value       = aws_instance.ubuntu_server.public_ip
}

output "instance_private_ip" {
  description = "Private IP address of the EC2 instance"
  value       = aws_instance.ubuntu_server.private_ip
}

output "ami_id" {
  description = "AMI ID used for Ubuntu 22.04 LTS"
  value       = data.aws_ami.ubuntu.id
}

output "instance_type" {
  description = "Instance type of the deployed EC2 server"
  value       = aws_instance.ubuntu_server.instance_type
}

output "region" {
  description = "AWS region"
  value       = var.aws_region
}
