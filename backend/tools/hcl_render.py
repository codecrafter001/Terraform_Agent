"""Safe, faithful HCL fragments for the adoption generator.

Everything discovered from AWS - names, tags, descriptions, policy documents -
is untrusted text (a tag value is whatever someone typed in the console), so
it only ever reaches HCL through hcl_str, which escapes quotes, backslashes,
newlines and Terraform's ${ / %{ template sequences. Nothing discovered can
close a string, start a new block, or interpolate.

Fidelity: tags_block, security_group_rules and route_lines render exactly
what is live, so an imported resource plans with no changes.
"""

from typing import Any, Callable, Dict, List, Optional, Tuple


def escape_template(text: str) -> str:
    return text.replace("${", "$${").replace("%{", "%%{")


def hcl_str(value: Any) -> str:
    text = "" if value is None else str(value)
    text = (text.replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    return f'"{escape_template(text)}"'


def hcl_bool(value: bool) -> str:
    return "true" if value else "false"


def tag_pairs(tags: Any) -> List[Tuple[str, str]]:
    """Live tags as sorted (key, value) pairs. aws:-prefixed tags are AWS's
    own and ignored by the provider, so they're never written."""
    pairs: List[Tuple[str, str]] = []
    if isinstance(tags, dict):
        pairs = [(str(k), "" if v is None else str(v)) for k, v in tags.items()]
    elif isinstance(tags, list):
        for t in tags:
            if isinstance(t, dict):
                key = t.get("Key", t.get("key"))
                if key is not None:
                    value = t.get("Value", t.get("value"))
                    pairs.append((str(key), "" if value is None else str(value)))
    return sorted(p for p in pairs if not p[0].lower().startswith("aws:"))


def tags_block(tags: Any) -> str:
    """The resource's exact live tags, or "" when it has none (omitting tags
    is then the no-change form)."""
    pairs = tag_pairs(tags)
    if not pairs:
        return ""
    lines = "\n".join(f"    {hcl_str(k)} = {hcl_str(v)}" for k, v in pairs)
    return f"\n  tags = {{\n{lines}\n  }}"


def _ports(perm: Dict[str, Any]) -> Tuple[int, int, str]:
    protocol = str(perm.get("IpProtocol", "-1"))
    if protocol == "-1":
        return 0, 0, protocol
    return int(perm.get("FromPort", 0) or 0), int(perm.get("ToPort", 0) or 0), protocol


def security_group_rules(
    perms: List[Dict[str, Any]], own_group_id: str, ref: Callable[[Optional[str]], str]
) -> List[List[str]]:
    """One inline rule per (permission, description), covering every source
    AWS allows: IPv4, IPv6, prefix lists, other groups and self. Leaving any
    of them out would make the plan delete that rule.

    Other groups are written as literal IDs, not references: two groups that
    allow each other would otherwise be a Terraform dependency cycle."""
    rules: List[List[str]] = []
    for perm in perms:
        from_port, to_port, protocol = _ports(perm)
        groups: Dict[str, Dict[str, Any]] = {}

        def bucket(description: Optional[str]) -> Dict[str, Any]:
            return groups.setdefault(description or "", {
                "cidr_blocks": [], "ipv6_cidr_blocks": [], "prefix_list_ids": [], "security_groups": [], "self": False,
            })

        for r in perm.get("IpRanges", []) or []:
            if r.get("CidrIp"):
                bucket(r.get("Description"))["cidr_blocks"].append(r["CidrIp"])
        for r in perm.get("Ipv6Ranges", []) or []:
            if r.get("CidrIpv6"):
                bucket(r.get("Description"))["ipv6_cidr_blocks"].append(r["CidrIpv6"])
        for r in perm.get("PrefixListIds", []) or []:
            if r.get("PrefixListId"):
                bucket(r.get("Description"))["prefix_list_ids"].append(r["PrefixListId"])
        for r in perm.get("UserIdGroupPairs", []) or []:
            gid = r.get("GroupId")
            if not gid:
                continue
            if gid == own_group_id:
                bucket(r.get("Description"))["self"] = True
            else:
                bucket(r.get("Description"))["security_groups"].append(gid)

        for description, sources in sorted(groups.items()):
            lines = [f"from_port   = {from_port}", f"to_port     = {to_port}", f"protocol    = {hcl_str(protocol)}"]
            for attr in ("cidr_blocks", "ipv6_cidr_blocks", "prefix_list_ids", "security_groups"):
                if sources[attr]:
                    lines.append(f"{attr} = [{', '.join(hcl_str(v) for v in sorted(sources[attr]))}]")
            if sources["self"]:
                lines.append("self = true")
            if description:
                lines.append(f"description = {hcl_str(description)}")
            rules.append(lines)
    return rules


# (discovered route field, aws_route_table route argument)
_ROUTE_DESTINATIONS = (
    ("destination_cidr_block", "cidr_block"),
    ("destination_ipv6_cidr_block", "ipv6_cidr_block"),
    ("destination_prefix_list_id", "destination_prefix_list_id"),
)
_ROUTE_TARGETS = (
    ("nat_gateway_id", "nat_gateway_id"),
    ("transit_gateway_id", "transit_gateway_id"),
    ("vpc_peering_connection_id", "vpc_peering_connection_id"),
    ("egress_only_internet_gateway_id", "egress_only_gateway_id"),
    ("carrier_gateway_id", "carrier_gateway_id"),
    ("local_gateway_id", "local_gateway_id"),
    ("core_network_arn", "core_network_arn"),
    ("network_interface_id", "network_interface_id"),
)


def route_lines(route: Dict[str, Any]) -> Optional[List[str]]:
    """Arguments for one inline route. None for routes Terraform doesn't
    manage inline (the implicit local route, VGW-propagated routes); [] when
    the destination or target can't be represented."""
    if route.get("gateway_id") == "local" or route.get("origin") in ("CreateRouteTable", "EnableVgwRoutePropagation"):
        return None
    lines: List[str] = []
    for field, arg in _ROUTE_DESTINATIONS:
        if route.get(field):
            lines.append(f"{arg} = {hcl_str(route[field])}")
            break
    else:
        return []
    gateway = route.get("gateway_id")
    if gateway:
        arg = "vpc_endpoint_id" if str(gateway).startswith("vpce-") else "gateway_id"
        lines.append(f"{arg} = {hcl_str(gateway)}")
        return lines
    for field, arg in _ROUTE_TARGETS:
        if route.get(field):
            lines.append(f"{arg} = {hcl_str(route[field])}")
            return lines
    return []
