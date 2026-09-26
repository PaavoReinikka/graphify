// Sample bicep for grammar introspection
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

module networking './modules/network.bicep' = {
  name: 'networkingDeploy'
  params: {
    location: location
  }
}

output storageId string = storage.id
output blobEndpoint string = storage.properties.primaryEndpoints.blob
