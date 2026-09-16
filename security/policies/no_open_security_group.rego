package main

# Sensitive administrative and database ports
sensitive_ports := [22, 3389, 5432, 3306, 27017, 6379]

# Deny unrestricted 0.0.0.0/0 ingress on sensitive ports
deny contains msg if {
    resource := input.resource.aws_security_group[_][_]
    ingress := resource.ingress[_]
    ingress.cidr_blocks[_] == "0.0.0.0/0"
    port := sensitive_ports[_]
    ingress.from_port <= port
    ingress.to_port >= port
    msg := sprintf("Security Group opens sensitive port %v to unrestricted 0.0.0.0/0", [port])
}
