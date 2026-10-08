"""AWS-native declarative Gateway template synthesized from the live catalog."""

from __future__ import annotations

from collections.abc import Mapping

from ai_dlc.application.gateway import GatewayCatalog
from ai_dlc.application.gateway.invocation import INVOCATION_REF_FIELD
from ai_dlc.application.tool_policy import ToolKind


def _schema(schema: Mapping[str, object], root: Mapping[str, object]) -> dict[str, object]:
    """Project Pydantic JSON Schema to AgentCore's limited SchemaDefinition."""
    if "$ref" in schema:
        key = str(schema["$ref"]).split("/")[-1]
        definitions = root.get("$defs", {})
        if key not in definitions:
            raise ValueError("unresolved tool schema reference")
        return _schema(definitions[key], root)
    if "anyOf" in schema:
        variants = [item for item in schema["anyOf"] if item.get("type") != "null"]
        if len(variants) != 1:
            raise ValueError("unsupported tool schema union")
        return _schema(variants[0], root)
    kind = schema.get("type")
    if kind not in {"string", "integer", "number", "boolean", "array", "object"}:
        raise ValueError("unsupported tool schema type")
    result: dict[str, object] = {"Type": kind}
    description = schema.get("description") or schema.get("title")
    if description:
        result["Description"] = str(description)
    if kind == "array":
        result["Items"] = _schema(schema["items"], root)
    if kind == "object":
        result["Properties"] = {
            name: _schema(child, root) for name, child in schema.get("properties", {}).items()
        }
        if schema.get("required"):
            result["Required"] = list(schema["required"])
    return result


def synthesize_gateway_template(catalog: GatewayCatalog) -> dict[str, object]:
    """No physical resource bindings or credentials enter the template."""
    params = {
        "Environment": {"Type": "String", "AllowedPattern": "[a-z][a-z0-9-]*"},
        "RuntimeRoleName": {
            "Type": "String",
            "Description": "Existing agent harness IAM role name",
        },
    }
    domains = {
        ToolKind.JIRA: ("Jira", "JiraTargetFunctionName", "JiraTargetRoleName"),
        ToolKind.SERVICENOW: (
            "ServiceNow",
            "ServiceNowTargetFunctionName",
            "ServiceNowTargetRoleName",
        ),
        ToolKind.GIT: ("RemoteGit", "GitTargetFunctionName", "GitTargetRoleName"),
    }
    for _, function_parameter, role_parameter in domains.values():
        params[function_parameter] = {
            "Type": "String",
            "AllowedPattern": "[a-zA-Z0-9_-]{1,64}",
        }
        params[role_parameter] = {"Type": "String", "AllowedPattern": "[a-zA-Z0-9_+=,.@-]{1,64}"}
    target_arns = [
        {
            "Fn::Sub": (
                "arn:${AWS::Partition}:lambda:${AWS::Region}:"
                f"${{AWS::AccountId}}:function:${{{item[1]}}}"
            )
        }
        for item in domains.values()
    ]
    resources: dict[str, object] = {
        "InvocationTable": {
            "Type": "AWS::DynamoDB::Table",
            "Properties": {
                "BillingMode": "PAY_PER_REQUEST",
                "AttributeDefinitions": [{"AttributeName": "reference_hash", "AttributeType": "S"}],
                "KeySchema": [{"AttributeName": "reference_hash", "KeyType": "HASH"}],
                "TimeToLiveSpecification": {"AttributeName": "expires_at", "Enabled": True},
                "SSESpecification": {"SSEEnabled": True},
            },
        },
        "GatewayRole": {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": "sts:AssumeRole",
                            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                            "Condition": {
                                "StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}},
                                "ArnLike": {
                                    "aws:SourceArn": {
                                        "Fn::Sub": (
                                            "arn:${AWS::Partition}:bedrock-agentcore:"
                                            "${AWS::Region}:${AWS::AccountId}:gateway/*"
                                        )
                                    }
                                },
                            },
                        }
                    ],
                },
                "Policies": [
                    {
                        "PolicyName": "InvokeApprovedEnterpriseTargets",
                        "PolicyDocument": {
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Effect": "Allow",
                                    "Action": ["lambda:InvokeFunction"],
                                    "Resource": target_arns,
                                }
                            ],
                        },
                    }
                ],
            },
        },
        "EnterpriseGateway": {
            "Type": "AWS::BedrockAgentCore::Gateway",
            "Properties": {
                "Name": {"Fn::Sub": "aidlc-${Environment}-tools"},
                "Description": "Governed AI-DLC enterprise tool gateway",
                "AuthorizerType": "AWS_IAM",
                "ProtocolType": "MCP",
                "RoleArn": {"Fn::GetAtt": ["GatewayRole", "Arn"]},
            },
        },
        "RuntimeGatewayInvokePolicy": {
            "Type": "AWS::IAM::Policy",
            "Properties": {
                "PolicyName": {"Fn::Sub": "aidlc-${Environment}-gateway-invoke"},
                "Roles": [{"Ref": "RuntimeRoleName"}],
                "PolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": ["bedrock-agentcore:InvokeGateway"],
                            "Resource": [{"Fn::GetAtt": ["EnterpriseGateway", "GatewayArn"]}],
                        }
                    ],
                },
            },
        },
        "RuntimeInvocationWritePolicy": {
            "Type": "AWS::IAM::Policy",
            "Properties": {
                "PolicyName": {"Fn::Sub": "aidlc-${Environment}-invocation-write"},
                "Roles": [{"Ref": "RuntimeRoleName"}],
                "PolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": ["dynamodb:PutItem"],
                            "Resource": [{"Fn::GetAtt": ["InvocationTable", "Arn"]}],
                        }
                    ],
                },
            },
        },
    }
    for domain, (label, parameter, role_parameter) in domains.items():
        definitions = []
        for tool in catalog.tools:
            if tool.domain is domain:
                definitions.append(
                    {
                        "Name": tool.name,
                        "Description": f"Governed {domain.value} {tool.operation.value}",
                        "InputSchema": _target_schema(tool.input_schema),
                    }
                )
        resources[f"{label}Target"] = {
            "Type": "AWS::BedrockAgentCore::GatewayTarget",
            "Properties": {
                "Name": label,
                "GatewayIdentifier": {"Ref": "EnterpriseGateway"},
                "CredentialProviderConfigurations": [
                    {"CredentialProviderType": "GATEWAY_IAM_ROLE"}
                ],
                "TargetConfiguration": {
                    "Mcp": {
                        "Lambda": {
                            "LambdaArn": {
                                "Fn::Sub": (
                                    "arn:${AWS::Partition}:lambda:${AWS::Region}:"
                                    f"${{AWS::AccountId}}:function:${{{parameter}}}"
                                )
                            },
                            "ToolSchema": {"InlinePayload": definitions},
                        }
                    }
                },
            },
        }
        resources[f"{label}InvocationReadPolicy"] = {
            "Type": "AWS::IAM::Policy",
            "Properties": {
                "PolicyName": {
                    "Fn::Sub": f"aidlc-${{Environment}}-{label.lower()}-invocation-read"
                },
                "Roles": [{"Ref": role_parameter}],
                "PolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": ["dynamodb:DeleteItem"],
                            "Resource": [{"Fn::GetAtt": ["InvocationTable", "Arn"]}],
                        }
                    ],
                },
            },
        }
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": f"AI-DLC AgentCore enterprise gateway catalog {catalog.version}",
        "Parameters": params,
        "Resources": resources,
        "Outputs": {
            "GatewayArn": {"Value": {"Fn::GetAtt": ["EnterpriseGateway", "GatewayArn"]}},
            "GatewayId": {"Value": {"Ref": "EnterpriseGateway"}},
            "JiraTargetId": {"Value": {"Fn::GetAtt": ["JiraTarget", "TargetId"]}},
            "ServiceNowTargetId": {"Value": {"Fn::GetAtt": ["ServiceNowTarget", "TargetId"]}},
            "RemoteGitTargetId": {"Value": {"Fn::GetAtt": ["RemoteGitTarget", "TargetId"]}},
            "Environment": {"Value": {"Ref": "Environment"}},
            "CatalogVersion": {"Value": catalog.version},
            "InvocationTableName": {"Value": {"Ref": "InvocationTable"}},
        },
    }


def _target_schema(schema: Mapping[str, object]) -> dict[str, object]:
    result = _schema(schema, schema)
    result["Properties"][INVOCATION_REF_FIELD] = {
        "Type": "string",
        "Description": "Opaque one-use reference injected by the authenticated runtime",
    }
    result["Required"] = [*result.get("Required", []), INVOCATION_REF_FIELD]
    return result
