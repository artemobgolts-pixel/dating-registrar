"""Явные возможности тестового клиента вместо характеристик CI/VPS-хоста."""

import json


def device_capabilities_script(*, device_memory=8, hardware_concurrency=8,
                               save_data=False):
    capabilities = json.dumps({"memory": device_memory,
                               "cores": hardware_concurrency,
                               "saveData": save_data})
    return """(() => {
      const capabilities = """ + capabilities + """;
      Object.defineProperties(navigator, {
        deviceMemory: {configurable: true, get: () => capabilities.memory},
        hardwareConcurrency: {configurable: true, get: () => capabilities.cores}
      });
      if (navigator.connection) {
        Object.defineProperty(navigator.connection, 'saveData', {
          configurable: true, get: () => capabilities.saveData
        });
      }
    })();"""
