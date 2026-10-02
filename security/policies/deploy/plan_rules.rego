package terraagent.deploy.plan

default allow = false

# Allow if there are no critical violations
allow {
    count(violations) == 0
}

# Rule: Disallow wildcard actions in IAM policies
violations[sprintf("IAM policy at %v contains wildcard Action '*'", [res.address])] {
    res := input.resource_changes[_]
    res.type == "aws_iam_role_policy"
    statement := res.change.after.policy.Statement[_]
    statement.Effect == "Allow"
    statement.Action == "*"
}

# Rule: Disallow unrestricted iam:* actions
violations[sprintf("IAM policy at %v contains broad iam:* action", [res.address])] {
    res := input.resource_changes[_]
    res.type == "aws_iam_role_policy"
    statement := res.change.after.policy.Statement[_]
    statement.Effect == "Allow"
    action := statement.Action[_]
    action == "iam:*"
}
