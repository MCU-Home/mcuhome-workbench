/*
 * SPDX-FileCopyrightText: 2026 The MCUHome Contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * MCUHome Matter sample node: minimal Matter node on vanilla Zephyr with ONE
 * dynamically registered temperature endpoint fed from a simulated sensor.
 * Evaluates the dynamic-endpoint path (builder-pipeline design §4) with
 * upstream CHIP on nRF7002-DK.
 */

#include <zephyr/drivers/gpio.h>
#include <zephyr/kernel.h>

#include <app-common/zap-generated/ids/Attributes.h>
#include <app-common/zap-generated/ids/Clusters.h>
#include <app/reporting/reporting.h>
#include <app/server/Dnssd.h>
#include <app/server/Server.h>
#include <app/util/attribute-storage.h>
#include <credentials/DeviceAttestationCredsProvider.h>
#include <credentials/examples/DeviceAttestationCredsExample.h>
#include <data-model-providers/codegen/Instance.h>
#include <lib/core/CHIPError.h>
#include <lib/support/CHIPMem.h>
#include <lib/support/Span.h>
#include <platform/CHIPDeviceLayer.h>

using namespace chip;
using namespace chip::app;
using namespace chip::app::Clusters;
using chip::Protocols::InteractionModel::Status;

/* MCUHome devices are native composed nodes, not bridges (ADR 0014): the
 * framework ZAP (components/matter/zap/mcuhome-root.zap) defines endpoint 0
 * — the root node — and nothing else. Every application endpoint is created
 * at runtime with emberAfSetDynamicEndpoint(), starting at EP1 directly
 * under the root; there is no static aggregator endpoint.
 *
 * A statically compiled endpoint would collide with the IDs the dynamic
 * allocator hands out and show up to controllers as a device the user never
 * configured. Fail the build, not the commissioning, if the ZAP ever grows
 * one. (FIXED_ENDPOINT_COUNT comes from the generated
 * zap-generated/endpoint_config.h, pulled in by app/util/attribute-storage.h
 * above — no fragile include path needed.) */
static_assert(FIXED_ENDPOINT_COUNT == 1,
              "The MCUHome framework ZAP must define endpoint 0 only — "
              "static endpoints collide with dynamically registered ones");

namespace {

constexpr int kDescriptorAttributeArraySize = 254;
constexpr uint16_t kTempSensorDeviceTypeId  = 0x0302; /* Matter Temperature Sensor */
constexpr uint8_t kDeviceVersionDefault     = 1;
/* EP0 (root node) is the only static endpoint; EP1 is the first dynamic one. */
constexpr EndpointId kTempEndpointId = 1;

int16_t gSimulatedTempCentiC = 2150; /* 21.50 °C, Matter unit: 0.01 °C */

/* LED status (dongle has no visible console): green solid = initializing,
 * red fast blink = a stage failed, green slow blink = fully up. */
const gpio_dt_spec gLedGreen = GPIO_DT_SPEC_GET(DT_ALIAS(led0), gpios);
const gpio_dt_spec gLedRed   = GPIO_DT_SPEC_GET(DT_ALIAS(led1), gpios);

/* --- Temperature Measurement cluster attributes (external storage) --- */
DECLARE_DYNAMIC_ATTRIBUTE_LIST_BEGIN(tempAttrs)
DECLARE_DYNAMIC_ATTRIBUTE(TemperatureMeasurement::Attributes::MeasuredValue::Id, INT16S, 2, 0),
    DECLARE_DYNAMIC_ATTRIBUTE(TemperatureMeasurement::Attributes::MinMeasuredValue::Id, INT16S, 2, 0),
    DECLARE_DYNAMIC_ATTRIBUTE(TemperatureMeasurement::Attributes::MaxMeasuredValue::Id, INT16S, 2, 0),
    DECLARE_DYNAMIC_ATTRIBUTE_LIST_END();

/* --- Descriptor cluster attributes --- */
DECLARE_DYNAMIC_ATTRIBUTE_LIST_BEGIN(descriptorAttrs)
DECLARE_DYNAMIC_ATTRIBUTE(Descriptor::Attributes::DeviceTypeList::Id, ARRAY, kDescriptorAttributeArraySize, 0),
    DECLARE_DYNAMIC_ATTRIBUTE(Descriptor::Attributes::ServerList::Id, ARRAY, kDescriptorAttributeArraySize, 0),
    DECLARE_DYNAMIC_ATTRIBUTE(Descriptor::Attributes::ClientList::Id, ARRAY, kDescriptorAttributeArraySize, 0),
    DECLARE_DYNAMIC_ATTRIBUTE(Descriptor::Attributes::PartsList::Id, ARRAY, kDescriptorAttributeArraySize, 0),
    DECLARE_DYNAMIC_ATTRIBUTE_LIST_END();

DECLARE_DYNAMIC_CLUSTER_LIST_BEGIN(tempSensorClusters)
DECLARE_DYNAMIC_CLUSTER(TemperatureMeasurement::Id, tempAttrs, ZAP_CLUSTER_MASK(SERVER), nullptr, nullptr),
    DECLARE_DYNAMIC_CLUSTER(Descriptor::Id, descriptorAttrs, ZAP_CLUSTER_MASK(SERVER), nullptr, nullptr)
        DECLARE_DYNAMIC_CLUSTER_LIST_END;

DECLARE_DYNAMIC_ENDPOINT(tempSensorEndpoint, tempSensorClusters);

DataVersion gTempDataVersions[MATTER_ARRAY_SIZE(tempSensorClusters)];

const EmberAfDeviceType gTempDeviceTypes[] = { { kTempSensorDeviceTypeId, kDeviceVersionDefault } };

} /* namespace */

/* Read handler for externally stored dynamic attributes. */
Status emberAfExternalAttributeReadCallback(EndpointId endpoint, ClusterId clusterId,
                                            const EmberAfAttributeMetadata * attributeMetadata, uint8_t * buffer,
                                            uint16_t maxReadLength)
{
    if (endpoint == kTempEndpointId && clusterId == TemperatureMeasurement::Id && maxReadLength >= 2)
    {
        int16_t value = 0;
        switch (attributeMetadata->attributeId)
        {
        case TemperatureMeasurement::Attributes::MeasuredValue::Id:
            value = gSimulatedTempCentiC;
            break;
        case TemperatureMeasurement::Attributes::MinMeasuredValue::Id:
            value = -4000;
            break;
        case TemperatureMeasurement::Attributes::MaxMeasuredValue::Id:
            value = 12500;
            break;
        default:
            return Status::Failure;
        }
        memcpy(buffer, &value, sizeof(value));
        return Status::Success;
    }
    return Status::Failure;
}

Status emberAfExternalAttributeWriteCallback(EndpointId endpoint, ClusterId clusterId,
                                             const EmberAfAttributeMetadata * attributeMetadata, uint8_t * buffer)
{
    return Status::Failure; /* sensor is read-only */
}

/* Diagnostic bring-up: log every stage, never halt — a panic would also
 * kill the USB console that is supposed to show us the failure. Before
 * each stage the red LED blinks the stage number, so a hang is readable
 * from the LED alone (no console needed). */
int gStageNo = 0;

void blink_stage_number(void)
{
    /* Bring-up phase used stage-counting blinks (~70 s total boot cost);
     * now that the stages are stable, a single short pulse suffices —
     * the failure path below still blinks distinctly. */
    ++gStageNo;
    gpio_pin_set_dt(&gLedRed, 1);
    k_sleep(K_MSEC(80));
    gpio_pin_set_dt(&gLedRed, 0);
}

#define STAGE(name, expr)                                                                                                          \
    do                                                                                                                             \
    {                                                                                                                              \
        blink_stage_number();                                                                                                      \
        printk("mcuhome-proto: %s...\n", name);                                                                                   \
        CHIP_ERROR _e;                                                                                                             \
        while ((_e = (expr)) != CHIP_NO_ERROR)                                                                                     \
        {                                                                                                                          \
            /* Retry forever: gives an attached debugger unlimited          \
             * reproduction windows without any reset dance. */             \
            printk("mcuhome-proto: %s FAILED: %" CHIP_ERROR_FORMAT " — retrying in 15 s\n", name, _e.Format());                   \
            for (int _i = 0; _i < 30; _i++)                                                                                        \
            {                                                                                                                      \
                gpio_pin_toggle_dt(&gLedRed);                                                                                      \
                k_sleep(K_MSEC(500));                                                                                              \
            }                                                                                                                      \
        }                                                                                                                          \
        gpio_pin_set_dt(&gLedRed, 0); /* stage done */                                                                             \
        printk("mcuhome-proto: %s OK\n", name);                                                                                   \
    } while (0)

int main(void)
{
    gpio_pin_configure_dt(&gLedGreen, GPIO_OUTPUT_INACTIVE);
    gpio_pin_configure_dt(&gLedRed, GPIO_OUTPUT_INACTIVE);
    gpio_pin_set_dt(&gLedGreen, 1); /* solid green: initializing */

    printk("mcuhome-proto: boot, image a2-composed-node\n");

    STAGE("MemoryInit", chip::Platform::MemoryInit());
    STAGE("InitChipStack", chip::DeviceLayer::PlatformMgr().InitChipStack());

    /* Attach CHIP's ThreadStackManager to the (Zephyr-L2-started) OpenThread
     * instance. Without this, Thread still runs — Zephyr starts it on its
     * own — but every CHIP feature needing the OT instance fails quietly;
     * the SRP hostname build then aborts DNS-SD advertising with NOT_FOUND
     * and the node is never discoverable for on-network commissioning. */
    STAGE("InitThreadStack", chip::DeviceLayer::ThreadStackMgr().InitThreadStack());
    STAGE("SetThreadDeviceType",
          chip::DeviceLayer::ConnectivityMgr().SetThreadDeviceType(
              chip::DeviceLayer::ConnectivityManager::kThreadDeviceType_Router));

    static chip::CommonCaseDeviceServerInitParams initParams;
    STAGE("InitStaticResources", initParams.InitializeStaticResourcesBeforeServerInit());
    initParams.dataModelProvider = chip::app::CodegenDataModelProviderInstance(initParams.persistentStorageDelegate);
    chip::Credentials::SetDeviceAttestationCredentialsProvider(chip::Credentials::Examples::GetExampleDACProvider());
    STAGE("ServerInit", chip::Server::GetInstance().Init(initParams));

    /* Register the dynamic temperature endpoint — this is the mechanism the
     * MCUHome builder will drive from generated config tables. */
    {
        chip::DeviceLayer::StackLock lock;
        STAGE("SetDynamicEndpoint", emberAfSetDynamicEndpoint(0, kTempEndpointId, &tempSensorEndpoint,
                                                               Span<DataVersion>(gTempDataVersions),
                                                               Span<const EmberAfDeviceType>(gTempDeviceTypes),
                                                               0 /* parent: root node */));
    }

    STAGE("StartEventLoop", chip::DeviceLayer::PlatformMgr().StartEventLoopTask());
    printk("mcuhome-proto: up — temperature endpoint %d live, simulating values\n", kTempEndpointId);

    /* Upstream gap (candidate C7): on the OpenThread platform nothing ever
     * posts kDnssdInitialized/kDnssdRestartNeeded, so a commissionable-node
     * advertisement that fails at ServerInit (SRP server not yet in the
     * Thread network data — normal with a pre-provisioned dataset) is never
     * retried and the node stays undiscoverable. Re-run StartServer()
     * periodically for the first ~5 minutes; re-registration is idempotent. */
    int dnssdRetries = 10;

    /* Simulated sensor: drift the value and trigger Matter reporting. */
    while (true)
    {
        if (dnssdRetries > 0)
        {
            dnssdRetries--;
            chip::DeviceLayer::PlatformMgr().ScheduleWork(
                [](intptr_t) { chip::app::DnssdServer::Instance().StartServer(); }, 0);
        }
        for (int i = 0; i < 30; i++)
        {
            gpio_pin_toggle_dt(&gLedGreen); /* slow green blink: fully up */
            k_sleep(K_SECONDS(1));
        }
        gSimulatedTempCentiC = static_cast<int16_t>(gSimulatedTempCentiC + 25);
        if (gSimulatedTempCentiC > 2600)
        {
            gSimulatedTempCentiC = 2000;
        }
        MatterReportingAttributeChangeCallback(kTempEndpointId, TemperatureMeasurement::Id,
                                               TemperatureMeasurement::Attributes::MeasuredValue::Id);
        printk("mcuhome-proto: alive, simulated temp %d.%02d C\n", gSimulatedTempCentiC / 100,
               gSimulatedTempCentiC % 100);
    }
    return 0;
}
