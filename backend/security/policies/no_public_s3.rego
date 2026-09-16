package terraagent.policies

default allow = false

# Rule 2: Deny public S3 ACLs
deny_public_s3 contains msg if {
    some resource in input.resource.aws_s3_bucket
    resource.acl == "public-read"
    msg := sprintf("S3 bucket '%v' has public-read ACL enabled", [resource])
}

deny_public_s3_write contains msg if {
    some resource in input.resource.aws_s3_bucket
    resource.acl == "public-read-write"
    msg := sprintf("S3 bucket '%v' has dangerous public-read-write ACL enabled", [resource])
}
