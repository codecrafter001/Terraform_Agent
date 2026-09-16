package terraagent.policies

default allow = false

# Rule 1: Deny 0.0.0.0/0 on sensitive ingress ports
deny_open_ports contains msg if {
    some resource in input.resource.aws_security_group
    some ingress in resource.ingress
    "0.0.0.0/0" in ingress.cidr_blocks
    sensitive_ports := [22, 3389, 5432, 3306, 27017, 6379]
    ingress.from_port in sensitive_ports
    msg := sprintf("Security group '%v' allows unrestricted ingress (0.0.0.0/0) on sensitive port %v", [resource, ingress.from_port])
}
