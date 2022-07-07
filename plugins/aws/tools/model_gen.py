import re
from attrs import define
from typing import List, Set, Optional, Tuple, Union, Dict

import boto3
from botocore.model import ServiceModel, StringShape, ListShape, Shape, StructureShape, MapShape


@define
class AwsProperty:
    name: str
    from_name: Union[str, List[str]]
    type: str
    description: str
    is_array: bool = False
    is_complex: bool = False
    field_default: Optional[str] = None
    extractor: Optional[str] = None

    def assignment(self) -> str:
        default = self.field_default or ("factory=list" if self.is_array else "default=None")
        return f"field({default})"

    def type_string(self) -> str:
        if self.is_array:
            return f"List[{self.type}]"
        else:
            return f"Optional[{self.type}]"

    def mapping(self) -> str:
        # in case an extractor is defined explicitly
        if self.extractor:
            return f'"{self.name}": {self.extractor}'
        from_p = self.from_name if isinstance(self.from_name, list) else [self.from_name]
        from_p_path = ",".join(f'"{p}"' for p in from_p)
        base = f'"{self.name}": S({from_p_path}'
        if self.is_array and self.is_complex:
            base += f", default=[]) >> ForallBend({self.type}.mapping)"
        elif self.is_array:
            base += ", default=[])"
        elif self.is_complex:
            base += f") >> Bend({self.type}.mapping)"
        else:
            base += ")"

        return base


@define
class AwsModel:
    name: str
    props: List[AwsProperty]
    aggregate_root: bool
    base_class: Optional[str] = None
    api_info: Optional[Tuple[str, str, str]] = None

    def to_class(self) -> str:
        bc = ", " + self.base_class if self.base_class else ""
        base = f"(AwsResource{bc}):" if self.aggregate_root else ":"
        kind = f'    kind: ClassVar[str] = "aws_{to_snake(self.name[3:])}"'
        if self.api_info:
            srv, act, res = self.api_info
            api = f'    api_info: ClassVar[AwsApiSpec] = AwsApiSpec("{srv}", "{act}", "{res}")\n'
        else:
            api = ""
        base_mapping = {
            "id": 'S("id")',
            "tags": 'S("Tags", default=[]) >> TagsToDict()',
            "name": 'S("Tags", default=[]) >> TagsValue("Name")',
            "ctime": "K(None)",
            "mtime": "K(None)",
            "atime": "K(None)",
        }
        mapping = "    mapping: ClassVar[Dict[str, Bender]] = {\n"
        if self.aggregate_root:
            mapping += ",\n".join(f'        "{k}": {v}' for k, v in base_mapping.items())
            mapping += ",\n"
        mapping += ",\n".join(f"        {p.mapping()}" for p in self.props)
        mapping += "\n    }"
        props = "\n".join(f"    {p.name}: {p.type_string()} = {p.assignment()}" for p in self.props)
        return f"@define(eq=False, slots=False)\nclass {self.name}{base}\n{kind}\n{api}{mapping}\n{props}\n"


@define
class AwsResotoModel:
    api_action: str  # action to perform on the client
    result_property: str  # this property holds the resulting list
    result_shape: str  # the shape of the result according to the service specification
    prefix: Optional[str] = None  # prefix for the resources
    prop_prefix: Optional[str] = None  # prefix for the attributes
    name: Optional[str] = None  # name of the clazz - uses the shape name by default
    base: Optional[str] = None  # the base class to use, BaseResource otherwise


def to_snake(name: str) -> str:
    name = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
    name = re.sub("__([A-Z])", r"_\1", name)
    name = re.sub("([a-z0-9])([A-Z])", r"\1_\2", name)
    return name.lower()


simple_type_map = {
    "Long": "int",
    "Float": "float",
    "Double": "float",
    "Integer": "int",
    "Boolean": "bool",
    "String": "str",
    "DateTime": "datetime",
    "Timestamp": "datetime",
    "TagsMap": "Dict[str, str]",
    "MillisecondDateTime": "datetime",
}
simple_type_map |= {k.lower(): v for k, v in simple_type_map.items()}

ignore_props = {"Tags", "tags"}


def service_model(name: str) -> ServiceModel:
    return boto3.client(name)._service_model


def clazz_model(
    model: ServiceModel,
    shape: Shape,
    visited: Set[str],
    prefix: Optional[str] = None,
    prop_prefix: Optional[str] = None,
    clazz_name: Optional[str] = None,
    base_class: Optional[str] = None,
    aggregate_root: bool = False,
    api_info: Optional[Tuple[str, str, str]] = None,
) -> List[AwsModel]:
    def type_name(s: Shape) -> str:
        spl = simple_shape(s)
        return spl if spl else f"Aws{prefix}{s.name}"

    def simple_shape(s: Shape) -> Optional[str]:
        if isinstance(s, StringShape):
            return "str"
        elif simple := simple_type_map.get(s.name):
            return simple
        elif simple := simple_type_map.get(s.type_name):
            return simple
        else:
            return None

    def complex_simple_shape(s: Shape) -> Optional[Tuple[str, str]]:
        # in case this shape is complex, but has only property of simple type, return that type
        if isinstance(s, StructureShape) and len(s.members) == 1:
            p_name, p_shape = next(iter(s.members.items()))
            p_simple = simple_shape(p_shape)
            return (p_name, p_simple) if p_simple else None
        else:
            return None

    if type_name(shape) in visited:
        return []
    visited.add(type_name(shape))
    result: List[AwsModel] = []
    props = []
    prefix = prefix or ""
    prop_prefix = prop_prefix or ""
    if isinstance(shape, StructureShape):
        for name, prop_shape in shape.members.items():
            prop = to_snake(name)
            if prop in ignore_props:
                continue
            if simple := simple_shape(prop_shape):
                props.append(AwsProperty(prop_prefix + prop, name, simple, prop_shape.documentation))
            elif isinstance(prop_shape, ListShape):
                inner = prop_shape.member
                if simple := simple_shape(inner):
                    props.append(AwsProperty(prop_prefix + prop, name, simple, prop_shape.documentation, is_array=True))
                elif simple_path := complex_simple_shape(inner):
                    prop_name, prop_type = simple_path
                    props.append(
                        AwsProperty(
                            prop_prefix + prop,
                            [name, prop_name],
                            prop_type,
                            prop_shape.documentation,
                            is_array=True,
                            extractor=f'S("{name}", default=[]) >> ForallBend(S("{prop_name}"))',
                        )
                    )

                else:
                    result.extend(clazz_model(model, inner, visited, prefix))
                    props.append(
                        AwsProperty(
                            prop_prefix + prop,
                            name,
                            type_name(inner),
                            prop_shape.documentation,
                            is_array=True,
                            is_complex=True,
                        )
                    )
            elif isinstance(prop_shape, MapShape):
                key_type = simple_shape(prop_shape.key)
                assert key_type, f"Key type must be a simple type: {prop_shape.key.name}"
                value_type = type_name(prop_shape.value)
                result.extend(clazz_model(model, prop_shape.value, visited, prefix))
                props.append(
                    AwsProperty(prop_prefix + prop, name, f"Dict[{key_type}, {value_type}]", prop_shape.documentation)
                )

            elif isinstance(prop_shape, StructureShape):
                if maybe_simple := complex_simple_shape(prop_shape):
                    s_prop_name, s_prop_type = maybe_simple
                    props.append(
                        AwsProperty(prop_prefix + prop, [name, s_prop_name], s_prop_type, prop_shape.documentation)
                    )
                else:
                    result.extend(clazz_model(model, prop_shape, visited, prefix))
                    props.append(
                        AwsProperty(
                            prop_prefix + prop, name, type_name(prop_shape), prop_shape.documentation, is_complex=True
                        )
                    )
            else:
                raise NotImplementedError(f"Unsupported shape: {prop_shape}")

        clazz_name = clazz_name if clazz_name else type_name(shape)
        result.append(AwsModel(clazz_name, props, aggregate_root, base_class, api_info))
    return result


def all_models() -> List[AwsModel]:
    visited: Set[str] = set()
    result: List[AwsModel] = []
    for name, endpoint in models.items():
        sm = service_model(name)
        for ep in endpoint:
            shape = sm.shape_for(ep.result_shape)
            result.extend(
                clazz_model(
                    sm,
                    shape,
                    visited,
                    aggregate_root=True,
                    clazz_name=ep.name,
                    base_class=ep.base,
                    prop_prefix=ep.prop_prefix,
                    prefix=ep.prefix,
                    api_info=(name, ep.api_action, ep.result_property),
                )
            )

    return result


models: Dict[str, List[AwsResotoModel]] = {
    "accessanalyzer": [
        # AwsResotoModel("list-analyzers", "analyzers", "AnalyzerSummary", prefix="AccessAnalyzer"),
    ],
    "acm": [
        # AwsResotoModel("list-certificates", "CertificateSummaryList", "CertificateSummary", prefix="ACM"),
    ],
    "acm-pca": [
        # AwsResotoModel(
        #     "list-certificate-authorities", "CertificateAuthorities", "CertificateAuthority", prefix="ACMPCA"
        # ),
    ],
    "alexaforbusiness": [],  # TODO: implement
    "amp": [
        # AwsResotoModel("list-workspaces", "workspaces", "WorkspaceSummary", prefix="Amp"),
    ],
    "amplify": [
        # AwsResotoModel("list-apps", "apps", "App", prefix="Amplify"),
    ],
    "apigateway": [
        # AwsResotoModel("get-vpc-links", "items", "VpcLink", prefix="ApiGateway"),
        # AwsResotoModel("get-sdk-types", "items", "SdkType", prefix="ApiGateway"),
        # AwsResotoModel("get-rest-apis", "items", "RestApi", prefix="ApiGateway"),
        # AwsResotoModel("get-domain-names", "items", "DomainName", prefix="ApiGateway"),
        # AwsResotoModel("get-client-certificates", "items", "ClientCertificate", prefix="ApiGateway"),
    ],
    "apigatewayv2": [
        # AwsResotoModel("get-domain-names", "Items", "DomainName", prefix="ApiGatewayV2"),
        # AwsResotoModel("get-apis", "Items", "Api", prefix="ApiGatewayV2"),
    ],
    "appconfig": [
        # AwsResotoModel("list-applications", "Items", "Application", prefix="AppConfig"),
    ],
    "appflow": [
        # AwsResotoModel("list-flows", "flows", "FlowDefinition", prefix="Appflow"),
        # AwsResotoModel("list-connectors", "connectors", "ConnectorDetail", prefix="Appflow"),
    ],
    "appintegrations": [
        # AwsResotoModel(
        #     "list-data-integrations", "DataIntegrations", "DataIntegrationSummary", prefix="AppIntegrations"
        # ),
        # AwsResotoModel("list-event-integrations", "EventIntegrations", "EventIntegration", prefix="AppIntegrations"),
    ],
    "application-insights": [
        # AwsResotoModel("list-applications", "ApplicationInfoList", "ApplicationInfo", prefix="ApplicationInsights"),
        # AwsResotoModel("list-problems", "ProblemList", "Problem", prefix="ApplicationInsights"),
    ],
    "applicationcostprofiler": [
        # AwsResotoModel(
        #     "list-report-definitions", "reportDefinitions", "ReportDefinition", prefix="ApplicationCostProfiler"
        # ),
    ],
    "appmesh": [
        # AwsResotoModel("list-meshes", "meshes", "MeshRef", prefix="AppMesh"),
    ],
    "apprunner": [
        # AwsResotoModel("list-services", "ServiceSummaryList", "ServiceSummary", prefix="AppRunner"),
        # AwsResotoModel("list-vpc-connectors", "VpcConnectors", "VpcConnector", prefix="AppRunner"),
        # AwsResotoModel("list-connections", "ConnectionSummaryList", "ConnectionSummary", prefix="AppRunner"),
        # AwsResotoModel(
        #     "list-auto-scaling-configurations",
        #     "AutoScalingConfigurationSummaryList",
        #     "AutoScalingConfigurationSummary",
        #     prefix="AppRunner",
        # ),
        # AwsResotoModel(
        #     "list-observability-configurations ",
        #     "ObservabilityConfigurationSummaryList",
        #     "ObservabilityConfigurationSummary",
        #     prefix="AppRunner",
        # ),
    ],
    "appstream": [
        # AwsResotoModel("describe-fleets", "Fleets", "Fleet", prefix="AppStream"),
        # AwsResotoModel("describe-stacks", "Stacks", "Stack", prefix="AppStream"),
        # AwsResotoModel("describe-images", "Images", "Image", prefix="AppStream"),
    ],
    "appsync": [
        # AwsResotoModel("list-graphql-apis", "graphqlApis", "GraphqlApi", prefix="AppSync"),
        # AwsResotoModel("list-domain-names", "domainNameConfigs", "DomainNameConfig", prefix="AppSync"),
    ],
    "athena": [
        # AwsResotoModel("list-data-catalogs", "DataCatalogsSummary", "DataCatalogSummary", prefix="Athena"),
    ],
    "autoscaling": [
        # AwsResotoModel(
        #     "describe_auto_scaling_groups", "AutoScalingGroupName", "AutoScalingGroup", prefix="AutoScaling"
        # ),
    ],
    "ec2": [
        # AwsResotoModel(
        #     "describe-instances",
        #     "Reservations",
        #     "Instance",
        #     base="BaseInstance",
        #     prefix="Ec2",
        #     prop_prefix="instance_",
        # ),
        # AwsResotoModel("describe-key-pairs", "KeyPairs", "KeyPairInfo", prefix="Ec2"),
        # AwsResotoModel("describe-volumes", "Volumes", "Volume", base="BaseVolume", prefix="Ec2"),
        # AwsResotoModel("describe_addresses", "Addresses", "Address", prefix="Ec2"),
        # AwsResotoModel(
        #     "describe_reserved_instances",
        #     "ReservedInstances",
        #     "ReservedInstances",
        #     prefix="Ec2",
        #     prop_prefix="reservation_",
        # ),
        # AwsResotoModel("describe-network-acls", "NetworkAcls", "NetworkAcl", prefix="Ec2"),
    ],
    "route53": [
        # AwsResotoModel("list_hosted_zones", "HostedZones", "HostedZone", prefix="Route53"),
    ],
    "iam": [
        # AwsResotoModel(
        #     "list-server-certificates",
        #     "ServerCertificateMetadataList",
        #     "ServerCertificateMetadata",
        #     prefix="Iam",
        #     prop_prefix="server_certificate_",
        # ),
        # AwsResotoModel(
        #     "list-policies",
        #     "Policies",
        #     "Policy",
        #     prefix="Iam",
        #     prop_prefix="policy_",
        # ),
        # AwsResotoModel(
        #     "list-groups",
        #     "Groups",
        #     "Group",
        #     prefix="Iam",
        #     prop_prefix="group_",
        # ),
        # AwsResotoModel(
        #     "list-roles",
        #     "Roles",
        #     "Role",
        #     prefix="Iam",
        #     prop_prefix="role_",
        # ),
        # AwsResotoModel(
        #     "list-users",
        #     "Users",
        #     "User",
        #     prefix="Iam",
        #     prop_prefix="user_",
        # ),
        AwsResotoModel(
            "list-access-keys",
            "AccessKeyMetadata",
            "AccessKeyMetadata",
            prefix="Iam",
            prop_prefix="access_key_",
        ),
        # AwsResotoModel(
        #     "list-access-keys-last-user",
        #     "AccessKeyLastUsed",
        #     "AccessKeyLastUsed",
        #     prefix="Iam",
        #     prop_prefix="access_key_",
        # ),
    ],
}


if __name__ == "__main__":
    for model in all_models():
        print(model.to_class())