cilium install \
  --set kubeProxyReplacement=true \
  --set k8sServiceHost=192.168.73.21 \
  --set k8sServicePort=6443 \
  --set ipam.mode=kubernetes \
  --set routingMode=tunnel \
  --set tunnelProtocol=vxlan \
  --set gatewayAPI.enabled=true
