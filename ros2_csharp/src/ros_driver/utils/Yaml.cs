using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using YamlDotNet.Serialization;
using YamlDotNet.Serialization.NamingConventions;
using YamlDotNet.RepresentationModel;

namespace RosDriver.Utils
{
    public class ActionValueMapping
    {
        public string Type { get; set; }
        public Dictionary<string, string> Goal { get; set; }
        public Dictionary<string, string> Feedback { get; set; }
        public Dictionary<string, string> Result { get; set; }

        public ActionValueMapping()
        {
            Goal = new Dictionary<string, string>();
            Feedback = new Dictionary<string, string>();
            Result = new Dictionary<string, string>();
        }
    }

    public class DeviceInfo
    {
        public Dictionary<string, string> StatusTypes { get; set; }
        public Dictionary<string, ActionValueMapping> ActionValueMappings { get; set; }

        public DeviceInfo()
        {
            StatusTypes = new Dictionary<string, string>();
            ActionValueMappings = new Dictionary<string, ActionValueMapping>();
        }
    }

    public class YamlConfigParser
    {
       
        private static void ExtractMappingSection(YamlMappingNode parentNode, string sectionName, Dictionary<string, string> targetDict)
        {
            var sectionKeyNode = new YamlScalarNode(sectionName);
            if (parentNode.Children.ContainsKey(sectionKeyNode))
            {
                var sectionNode = parentNode.Children[sectionKeyNode] as YamlMappingNode;
                if (sectionNode != null)
                {
                    foreach (var entry in sectionNode.Children)
                    {
                        if (entry.Key is YamlScalarNode entryKeyNode && 
                            entry.Value is YamlScalarNode entryValueNode)
                        {
                            targetDict[entryKeyNode.Value] = entryValueNode.Value;
                        }
                    }
                }
            }
        }

        private static Dictionary<string, DeviceInfo> ParseDeviceInfo(string filePath)
        {
            var devices = new Dictionary<string, DeviceInfo>();

            using (var reader = new StreamReader(filePath))
            {
                var yaml = new YamlStream();
                yaml.Load(reader);

                var rootNode = yaml.Documents[0].RootNode as YamlMappingNode;
                if (rootNode == null)
                {
                    throw new InvalidDataException("Root node is not a mapping node.");
                }

                foreach (var deviceEntry in rootNode.Children)
                {
                    var deviceKey = ((YamlScalarNode)deviceEntry.Key).Value;
                    var deviceNode = deviceEntry.Value as YamlMappingNode;
                    if (deviceNode == null) continue;

                    var deviceInfo = new DeviceInfo();

                    // Parse status_types
                    YamlMappingNode classMappingNode = null;
                    if (deviceNode.Children.TryGetValue(new YamlScalarNode("class"), out var classNode) &&
                        classNode is YamlMappingNode tempClassNode)
                    {
                        classMappingNode = tempClassNode;
                        if (classMappingNode.Children.TryGetValue(new YamlScalarNode("status_types"), out var statusTypesNode) &&
                            statusTypesNode is YamlMappingNode statusTypesMappingNode)
                        {
                            foreach (var statusEntry in statusTypesMappingNode.Children)
                            {
                                var key = ((YamlScalarNode)statusEntry.Key).Value;
                                var value = ((YamlScalarNode)statusEntry.Value).Value;
                                deviceInfo.StatusTypes[key] = value;
                            }
                        }
                    }

                    // Parse action_value_mappings
                    if (classMappingNode != null &&
                        classMappingNode.Children.TryGetValue(new YamlScalarNode("action_value_mappings"), out var actionMappingsNode) &&
                        actionMappingsNode is YamlMappingNode actionMappingsMappingNode)
                    {
                        foreach (var actionEntry in actionMappingsMappingNode.Children)
                        {
                            var actionKey = ((YamlScalarNode)actionEntry.Key).Value;
                            var actionNode = actionEntry.Value as YamlMappingNode;
                            if (actionNode == null) continue;

                            var actionValue = new ActionValueMapping();

                            // Extract type
                            if (actionNode.Children.TryGetValue(new YamlScalarNode("type"), out var typeNode) &&
                                typeNode is YamlScalarNode typeScalarNode)
                            {
                                actionValue.Type = typeScalarNode.Value;
                            }

                            // Extract goal, feedback, result
                            ExtractMappingSection(actionNode, "goal", actionValue.Goal);
                            ExtractMappingSection(actionNode, "feedback", actionValue.Feedback);
                            ExtractMappingSection(actionNode, "result", actionValue.Result);

                            deviceInfo.ActionValueMappings[actionKey] = actionValue;
                        }
                    }

                    devices[deviceKey] = deviceInfo;
                }
            }

            return devices;
        }

        public static Dictionary<string, DeviceInfo> GetDeviceInfoDict(string filePath)
        {
            return ParseDeviceInfo(filePath);
        }
    }
}
