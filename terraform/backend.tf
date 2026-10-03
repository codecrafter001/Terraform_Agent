terraform {

  backend "s3" {
    bucket         = "terraagent-tf-state-us-east-1-nff6pk"
    key            = "environments/production/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "terraagent-tf-locks"
    encrypt        = true
  }
}
