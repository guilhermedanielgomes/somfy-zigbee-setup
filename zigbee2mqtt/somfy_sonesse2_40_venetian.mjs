import * as m from 'zigbee-herdsman-converters/lib/modernExtend';

export default {
    zigbeeModel: ['Sonesse2 40 Zigbee Venetian'],
    model: 'Sonesse2 40 Zigbee Venetian',
    vendor: 'Somfy',
    description: 'Sonesse2 40 Zigbee venetian blind motor',
    // the motor lists endpoint 232 first, but only endpoint 1 has the window covering cluster
    endpoint: () => ({default: 1}),
    extend: [m.windowCovering({controls: ['lift', 'tilt']}), m.identify()],
};
