// Sample Bicep fixture for the extractor tests
@description('The Azure region')
param location string = resourceGroup().location
param storageName string

var storageSku = 'Standard_LRS'

resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageName
  location: location
  sku: {
    name: storageSku
  }
  kind: 'StorageV2'
}

resource blob 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: storage
  name: 'default'
}

resource existingVnet 'Microsoft.Network/virtualNetworks@2023-01-01' existing = {
  name: 'shared-vnet'
}

resource nic 'Microsoft.Network/networkInterfaces@2023-01-01' = {
  name: 'nic-${location}'
  dependsOn: [
    storage
    existingVnet
  ]
}

resource vms 'Microsoft.Compute/virtualMachines@2023-01-01' = [for i in range(0, 3): {
  name: 'vm-${i}'
  dependsOn: [
    nic
  ]
}]

module networking './modules/network.bicep' = {
  name: 'networkingDeploy'
  params: {
    location: location
  }
}

output storageId string = storage.id
output blobEndpoint string = storage.properties.primaryEndpoints.blob
