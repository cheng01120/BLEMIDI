# bleak, python-rtmidi, midiutil
import argparse
import asyncio
import logging
import time
import sys


from bleak import BleakClient, BleakScanner

# Use VirtualMIDISynth instead of MS GS Wavetable
import rtmidi  # pip install python-rtmidi
from rtmidi.midiutil import open_midiport

from midiutil import MIDIFile


logger = logging.getLogger(__name__)


bpm       = 120
file_name = "Track1"
T_start   = time.time() # program start time

def quarter():
    return 1000.0 * 60 / bpm

class DeviceNotFoundError(Exception):
    pass

async def run_ble_client(args: argparse.Namespace, queue: asyncio.Queue):
    logger.info("starting scan...")

    device = await BleakScanner.find_device_by_name( "WU-BT10 MIDI", cb=dict(use_bdaddr=args.macos_use_bdaddr))
    if device is None:
        logger.error("could not find device with name WU-BT10 MIDI")
        raise DeviceNotFoundError

    logger.info("connecting to device...")

    async def callback_handler(_, data):
        await queue.put( ( 1000 * (time.time() - T_start),  data))

    async with BleakClient(device) as client:
        try:
            logger.info("Connected, press Enter to quit recording...")
            T_start = time.time() # program start time
            characteristic="7772e5db-3868-4112-a1a9-f2669d106bf3"
            await client.start_notify(characteristic, callback_handler)
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, sys.stdin.buffer.readline)
            await client.stop_notify(characteristic)
            # Send an "exit command to the consumer"
            await queue.put( ( (time.time() - T_start)* 1000, None) )
        except Exception as e:
            logger.error("Unable to read from WU-BT10: %s", repr(e))

    logger.info("disconnected")
        

async def run_queue_consumer(queue: asyncio.Queue):
    logger.info("Starting queue consumer")

    T_cycle0 = 0 # 第一个cycle开始的时间。
    E_ON = []    # Save NoteOn event

    midi_file = MIDIFile(1)
    midi_file.addTrackName(0, 0, file_name)
    midi_file.addTempo(0, 0, bpm)

    midi_out = rtmidi.MidiOut()
    midi_out.open_port(0)
    logger.info("Opening midi port %s for sending...", midi_out.get_port_name(0))


    while True:
        # Use await asyncio.wait_for(queue.get(), timeout=1.0) if you want a timeout for getting data.
        T_elapsed, data = await queue.get()
        if data is None:
            logger.info(
                "Got message from client about disconnection. Exiting consumer loop..."
            )
            midi_out.close_port()
            # And write it to disk.
            if len(E_ON) != 0:
                logger.info("%d unmatched note on", len(E_ON))
            with open(file_name + ".mid", "wb") as output_file:
                midi_file.writeFile(output_file)
            break
        else:
            logger.debug("Received callback data via async queue at %s: %r", T_elapsed, data)
            ss = (len(data) -1) & 0x03
            if ss != 0:
                logger.info("Cannot parse data size %d", len(data))
                continue

            high = data[0] & 0x3f
            low0 = data[1] & 0x7f

            N = 0
            for a in range(1, len(data) - 1, 4):
                low = data[a] & 0x7f
                if(low < low0):
                    high += 1
                T_packet = high << 7 | low
                low0 = low
                if T_cycle0 == 0:
                    T_cycle0 = T_elapsed - T_packet
                else:
                    N = (T_elapsed - T_cycle0) // 8192

                # time correction. 
                DT = (T_elapsed - T_cycle0) % 8192
                M  = N
                if abs(DT - T_packet) > 4096:
                    logger.debug("TIME ERROR: N: %d DT: %d T: %d", N, DT, T_packet);
                    if(T_packet > DT):
                        M = N - 1  # |            T | DT           |
                    else:
                        M = N + 1  # |            DT| T            |
                T_event = T_cycle0 + 8192 * M + T_packet   #将程序开始的时间设置为midi文件的0点。

                channel = data[1+a] & 0x0f
                if channel != 0:
                    continue   # only parse channel 0

                status = data[a+1] & 0xf0
                match status:
                    case 0x80: # note off
                        count = 0 #查找对应的Note On
                        for i, ev in enumerate(E_ON):
                            if ev[1] == data[a+2]:
                                midi_out.send_message([0x80, data[a+2], data[a+3]])
                                midi_file.addNote(
                                    0, data[a+1] & 0x0f, ev[1], ev[0]/quarter(), (T_event - ev[0])/quarter(), ev[2])
                                E_ON.pop(i)
                                count += 1
                                break
                        if count == 0:
                            logger.info("Unmatched note off %d at %d", data[a+2], T_event)

                    case 0x90: # note on
                        dup = 0 # 检查重复的note on
                        for ee in E_ON:
                            if(ee[1] == data[a+2]):
                                logger.info("Dup note on %d at T:%d - %d V: %d - %d", ee[1], ee[0], T_event, ee[2], data[a+3])
                                dup += 1
                        if(dup == 0): #忽略重复的Note On
                            midi_out.send_message([0x90, data[a+2], data[a+3]])
                            E_ON.append([T_event, data[a+2], data[a+3]])

                    case 0xb0: # controller
                        midi_out.send_message([0xb0, data[a+2], data[a+3]])
                        midi_file.addControllerEvent(0, data[a+1] & 0x0f, T_event/quarter(), data[a+2], data[a+3])

                    case _:
                        pass

async def main(args: argparse.Namespace):
    queue = asyncio.Queue()
    client_task = run_ble_client(args, queue)
    consumer_task = run_queue_consumer(queue)

    try:
        await asyncio.gather(client_task, consumer_task)
    except DeviceNotFoundError:
        pass

    logger.info("Main method done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--macos-use-bdaddr",
        action="store_true",
        help="when true use Bluetooth address instead of UUID on macOS",
    )

    parser.add_argument(
        "-t",
        "--tempo",
        type=int,
        default=120,
        help="sets the MIDI file tempo(BPM)",
    )

    parser.add_argument(
        "-n",
        "--name",
        type=str,
        default="",
        help="sets the MIDI file name",
    )

    parser.add_argument(
        "-d", "--debug",
        action="store_true",
        help="sets the logging level to debug",
    )

    args = parser.parse_args()
    bpm = args.tempo
    if(len(args.name)):
        file_name = args.name

    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)-15s %(name)-8s %(levelname)s: %(message)s",
    )


    asyncio.run(main(args))
