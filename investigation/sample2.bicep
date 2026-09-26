param env string

resource existingVnet 'Microsoft.Network/virtualNetworks@2023-01-01' existing = {
  name: 'shared-vnet'
}

resource nsg 'Microsoft.Network/networkSecurityGroups@2023-01-01' = {
  name: 'nsg-${env}'
  location: 'eastus'
}

resource nic 'Microsoft.Network/networkInterfaces@2023-01-01' = {
  name: 'nic1'
  dependsOn: [
    nsg
    existingVnet
  ]
  properties: {
    networkSecurityGroup: {
      id: nsg.id
    }
  }
}

resource vms 'Microsoft.Compute/virtualMachines@2023-01-01' = [for i in range(0, 3): {
  name: 'vm-${i}'
  dependsOn: [
    nic
  ]
}]
