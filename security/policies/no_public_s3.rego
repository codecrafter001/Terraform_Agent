package main

# Deny S3 buckets with public-read or public-read-write ACLs
deny contains msg if {
    resource := input.resource.aws_s3_bucket[_][_]
    resource.acl == "public-read"
    msg := "S3 bucket configuration has prohibited 'public-read' ACL"
}

deny contains msg if {
    resource := input.resource.aws_s3_bucket[_][_]
    resource.acl == "public-read-write"
    msg := "S3 bucket configuration has prohibited 'public-read-write' ACL"
}
