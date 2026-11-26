package com.portaldash.shell

import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothGattServer
import android.bluetooth.BluetoothGattServerCallback
import android.bluetooth.BluetoothGattService
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.le.AdvertiseCallback
import android.bluetooth.le.AdvertiseData
import android.bluetooth.le.AdvertiseSettings
import android.content.Context
import android.os.Handler
import android.os.Looper
import android.os.ParcelUuid
import android.util.Log
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.util.ArrayDeque
import java.util.UUID

/**
 * Lends the Portal's Bluetooth radio to the Mac for pairing with the Muse app.
 *
 * The Mac (muse/muse.py pair) runs the pairing protocol; this only carries its bytes. It
 * connects to the Mac, which says which GATT service to advertise, and then passes on
 * every write from the phone and sends every notification the Mac asks for. Messages are
 * one JSON object per line, with payloads in hex. Nothing is advertised except while the
 * Mac holds the connection open.
 */
@SuppressLint("MissingPermission") // BLUETOOTH and BLUETOOTH_ADMIN are install-time on API 29.
class BleRelay(context: Context) {

    private val context = context.applicationContext
    private val main = Handler(Looper.getMainLooper())
    private val manager = this.context.getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager
    private val adapter: BluetoothAdapter? = manager.adapter

    private var socket: Socket? = null
    private var out: OutputStream? = null

    private var server: BluetoothGattServer? = null
    private var tx: BluetoothGattCharacteristic? = null
    private var phone: BluetoothDevice? = null
    private var savedName: String? = null
    private var advertising = false

    // Android sends one notification at a time; the rest wait for onNotificationSent.
    private val pending = ArrayDeque<ByteArray>()
    private var sending = false
    private val prepared = ByteArrayOutputStream()

    /** Connects to the Mac at "host:port" and serves it until either side hangs up. */
    fun start(target: String) {
        stop()
        val host = target.substringBeforeLast(':')
        val port = target.substringAfterLast(':').toIntOrNull() ?: return
        Thread({ serve(host, port) }, "ble-relay").start()
    }

    fun stop() {
        try {
            socket?.close()
        } catch (e: IOException) {
            // Already closed.
        }
    }

    private fun serve(host: String, port: Int) {
        val connection = Socket()
        try {
            connection.connect(InetSocketAddress(host, port), CONNECT_TIMEOUT_MS)
            socket = connection
            out = connection.getOutputStream()
            connection.getInputStream().bufferedReader().forEachLine { line ->
                val message = JSONObject(line)
                main.post { handle(message) }
            }
        } catch (e: Exception) {
            Log.w(TAG, "relay ended: ${e.javaClass.simpleName}")
        } finally {
            try {
                connection.close()
            } catch (e: IOException) {
                // Already closed.
            }
            main.post { teardown() }
        }
    }

    private fun send(event: String, fill: JSONObject.() -> Unit = {}) {
        val line = JSONObject().put("ev", event).apply(fill).toString() + "\n"
        val stream = out ?: return
        // Called from Bluetooth callbacks; keep socket writes off the main thread.
        Thread {
            try {
                synchronized(stream) { stream.write(line.toByteArray()) }
            } catch (e: IOException) {
                stop()
            }
        }.start()
    }

    // ---------- Commands from the Mac (main thread) ----------

    private fun handle(message: JSONObject) {
        when (message.optString("op")) {
            "start" -> open(message)
            "notify" -> {
                pending.add(unhex(message.getString("data")))
                pump()
            }
            "disconnect" -> phone?.let { server?.cancelConnection(it) }
        }
    }

    private fun open(message: JSONObject) {
        val adapter = adapter
        if (adapter == null || !adapter.isEnabled) {
            send("error") { put("message", "Bluetooth is off") }
            return
        }
        val serviceId = UUID.fromString(message.getString("service"))
        val service = BluetoothGattService(serviceId, BluetoothGattService.SERVICE_TYPE_PRIMARY)
        service.addCharacteristic(
            BluetoothGattCharacteristic(
                UUID.fromString(message.getString("rx")),
                BluetoothGattCharacteristic.PROPERTY_WRITE or
                    BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE,
                BluetoothGattCharacteristic.PERMISSION_WRITE,
            ),
        )
        val notify = BluetoothGattCharacteristic(
            UUID.fromString(message.getString("tx")),
            BluetoothGattCharacteristic.PROPERTY_READ or BluetoothGattCharacteristic.PROPERTY_NOTIFY,
            BluetoothGattCharacteristic.PERMISSION_READ,
        )
        notify.addDescriptor(
            BluetoothGattDescriptor(
                CLIENT_CONFIG,
                BluetoothGattDescriptor.PERMISSION_READ or BluetoothGattDescriptor.PERMISSION_WRITE,
            ),
        )
        service.addCharacteristic(notify)
        tx = notify

        val gatt = manager.openGattServer(context, gattCallback)
        if (gatt == null || !gatt.addService(service)) {
            send("error") { put("message", "could not open a GATT server") }
            return
        }
        server = gatt

        // The phone reads the adapter's name as well as the advertised one.
        savedName = adapter.name
        adapter.name = message.getString("name")
        val company = message.optInt("company", 0xFFFF)
        // Renaming takes a moment to reach the advertiser.
        main.postDelayed({ advertise(serviceId, company) }, RENAME_SETTLE_MS)
    }

    private fun advertise(serviceId: UUID, company: Int) {
        val advertiser = adapter?.bluetoothLeAdvertiser
        if (advertiser == null || server == null) {
            send("error") { put("message", "this device cannot advertise") }
            return
        }
        val settings = AdvertiseSettings.Builder()
            .setAdvertiseMode(AdvertiseSettings.ADVERTISE_MODE_LOW_LATENCY)
            .setTxPowerLevel(AdvertiseSettings.ADVERTISE_TX_POWER_HIGH)
            .setConnectable(true)
            .build()
        // The service and the "not paired" flag fit in the advertisement; the name goes in
        // the scan response.
        val data = AdvertiseData.Builder()
            .addServiceUuid(ParcelUuid(serviceId))
            .addManufacturerData(company, byteArrayOf(0))
            .build()
        val response = AdvertiseData.Builder().setIncludeDeviceName(true).build()
        advertising = true
        advertiser.startAdvertising(settings, data, response, advertiseCallback)
    }

    private fun pump() {
        val device = phone
        val characteristic = tx
        val gatt = server
        if (device == null || characteristic == null || gatt == null) {
            pending.clear()
            sending = false
            return
        }
        if (sending) return
        val packet = pending.poll() ?: return
        characteristic.value = packet
        @Suppress("DEPRECATION") // The replacement needs API 33; Portals stop at 29.
        sending = gatt.notifyCharacteristicChanged(device, characteristic, false)
        if (!sending) main.post { pump() }
    }

    private fun teardown() {
        main.removeCallbacksAndMessages(null)
        if (advertising) {
            adapter?.bluetoothLeAdvertiser?.stopAdvertising(advertiseCallback)
            advertising = false
        }
        server?.close()
        server = null
        tx = null
        phone = null
        pending.clear()
        sending = false
        prepared.reset()
        savedName?.let { adapter?.name = it }
        savedName = null
        socket = null
        out = null
    }

    // ---------- Bluetooth callbacks (binder threads; hop to main) ----------

    private val advertiseCallback = object : AdvertiseCallback() {
        override fun onStartSuccess(settingsInEffect: AdvertiseSettings) {
            send("advertising")
        }

        override fun onStartFailure(errorCode: Int) {
            send("error") { put("message", "advertising failed ($errorCode)") }
        }
    }

    private val gattCallback = object : BluetoothGattServerCallback() {

        override fun onConnectionStateChange(device: BluetoothDevice, status: Int, newState: Int) {
            main.post {
                if (newState == BluetoothProfile.STATE_CONNECTED) {
                    phone = device
                    send("connected")
                } else if (newState == BluetoothProfile.STATE_DISCONNECTED && device == phone) {
                    phone = null
                    pending.clear()
                    sending = false
                    prepared.reset()
                    send("disconnected")
                }
            }
        }

        override fun onMtuChanged(device: BluetoothDevice, mtu: Int) {
            send("mtu") { put("mtu", mtu) }
        }

        override fun onCharacteristicWriteRequest(
            device: BluetoothDevice,
            requestId: Int,
            characteristic: BluetoothGattCharacteristic,
            preparedWrite: Boolean,
            responseNeeded: Boolean,
            offset: Int,
            value: ByteArray,
        ) {
            main.post {
                if (preparedWrite) {
                    // A long write arrives in pieces and is delivered on onExecuteWrite.
                    if (offset == 0) prepared.reset()
                    prepared.write(value)
                } else {
                    send("write") { put("data", hex(value)) }
                }
                if (responseNeeded) {
                    server?.sendResponse(device, requestId, BluetoothGatt.GATT_SUCCESS, offset, value)
                }
            }
        }

        override fun onExecuteWrite(device: BluetoothDevice, requestId: Int, execute: Boolean) {
            main.post {
                if (execute && prepared.size() > 0) {
                    send("write") { put("data", hex(prepared.toByteArray())) }
                }
                prepared.reset()
                server?.sendResponse(device, requestId, BluetoothGatt.GATT_SUCCESS, 0, null)
            }
        }

        override fun onCharacteristicReadRequest(
            device: BluetoothDevice,
            requestId: Int,
            offset: Int,
            characteristic: BluetoothGattCharacteristic,
        ) {
            main.post {
                val value = characteristic.value ?: ByteArray(0)
                val rest = if (offset < value.size) value.copyOfRange(offset, value.size) else ByteArray(0)
                server?.sendResponse(device, requestId, BluetoothGatt.GATT_SUCCESS, offset, rest)
            }
        }

        override fun onDescriptorReadRequest(
            device: BluetoothDevice,
            requestId: Int,
            offset: Int,
            descriptor: BluetoothGattDescriptor,
        ) {
            main.post {
                val value = descriptor.value ?: BluetoothGattDescriptor.DISABLE_NOTIFICATION_VALUE
                server?.sendResponse(device, requestId, BluetoothGatt.GATT_SUCCESS, 0, value)
            }
        }

        override fun onDescriptorWriteRequest(
            device: BluetoothDevice,
            requestId: Int,
            descriptor: BluetoothGattDescriptor,
            preparedWrite: Boolean,
            responseNeeded: Boolean,
            offset: Int,
            value: ByteArray,
        ) {
            main.post {
                descriptor.value = value
                if (responseNeeded) {
                    server?.sendResponse(device, requestId, BluetoothGatt.GATT_SUCCESS, 0, null)
                }
                if (value.contentEquals(BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE)) {
                    send("subscribed")
                }
            }
        }

        override fun onNotificationSent(device: BluetoothDevice, status: Int) {
            main.post {
                sending = false
                pump()
            }
        }
    }

    private companion object {
        const val TAG = "BleRelay"
        const val CONNECT_TIMEOUT_MS = 5_000
        const val RENAME_SETTLE_MS = 1_000L

        // Client Characteristic Configuration: how a phone subscribes to notifications.
        val CLIENT_CONFIG: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")

        fun hex(bytes: ByteArray): String = bytes.joinToString("") { "%02x".format(it) }

        fun unhex(text: String): ByteArray =
            ByteArray(text.length / 2) { text.substring(it * 2, it * 2 + 2).toInt(16).toByte() }
    }
}
