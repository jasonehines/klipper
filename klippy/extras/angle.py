# Support for reading SPI magnetic angle sensors
#
# Copyright (C) 2021  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging, math, threading
from . import bus, motion_report

MIN_MSG_TIME = 0.100
TCODE_ERROR = 0xff

TRINAMIC_DRIVERS = ["tmc2130", "tmc2208", "tmc2209", "tmc2660", "tmc5160"]

CALIBRATION_BITS = 6 # 64 entries
ANGLE_BITS = 16 # angles range from 0..65535

class AngleCalibration:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name()
        self.stepper_name = config.get('stepper', None)
        if self.stepper_name is None:
            # No calibration
            return
        try:
            import numpy
        except:
            raise config.error("Angle calibration requires numpy module")
        sconfig = config.getsection(self.stepper_name)
        sconfig.getint('microsteps', note_valid=False)
        self.tmc_module = self.mcu_stepper = None
        # Current calibration data
        self.mcu_pos_offset = None
        self.angle_phase_offset = 0.
        self.calibration_reversed = False
        self.calibration = []
        cal = config.get('calibrate', None)
        if cal is not None:
            data = [d.strip() for d in cal.split(',')]
            angles = [float(d) for d in data if d]
            self.load_calibration(angles)
        # Register commands
        self.printer.register_event_handler("stepper:sync_mcu_position",
                                            self.handle_sync_mcu_pos)
        self.printer.register_event_handler("klippy:connect", self.connect)
        gcode = self.printer.lookup_object('gcode')
        gcode.register_mux_command("ANGLE_CALIBRATE", "STEPPER",
                                   self.stepper_name, self.cmd_ANGLE_CALIBRATE,
                                   desc=self.cmd_ANGLE_CALIBRATE_help)
    def handle_sync_mcu_pos(self, mcu_stepper):
        if mcu_stepper.get_name() == self.stepper_name:
            self.mcu_pos_offset = None
    def calc_mcu_pos_offset(self, sample):
        # Lookup phase information
        mcu_phase_offset, phases = self.tmc_module.get_phase_offset()
        if mcu_phase_offset is None:
            return
        # Find mcu position at time of sample
        angle_time, angle_pos = sample
        mcu_pos = self.mcu_stepper.get_past_mcu_position(angle_time)
        # Convert angle_pos to mcu_pos units
        microsteps, full_steps = self.get_microsteps()
        angle_to_mcu_pos = full_steps * microsteps / float(1<<ANGLE_BITS)
        angle_mpos = angle_pos * angle_to_mcu_pos
        # Calculate adjustment for stepper phases
        phase_diff = ((angle_mpos + self.angle_phase_offset * angle_to_mcu_pos)
                      - (mcu_pos + mcu_phase_offset)) % phases
        if phase_diff > phases//2:
            phase_diff -= phases
        # Store final offset
        self.mcu_pos_offset = mcu_pos - (angle_mpos - phase_diff)
    def apply_calibration(self, samples):
        calibration = self.calibration
        if not calibration:
            return None
        calibration_reversed = self.calibration_reversed
        interp_bits = ANGLE_BITS - CALIBRATION_BITS
        interp_mask = (1 << interp_bits) - 1
        interp_round = 1 << (interp_bits - 1)
        for i, (samp_time, angle) in enumerate(samples):
            bucket = (angle & 0xffff) >> interp_bits
            cal1 = calibration[bucket]
            cal2 = calibration[bucket + 1]
            adj = (angle & interp_mask) * (cal2 - cal1)
            adj = cal1 + ((adj + interp_round) >> interp_bits)
            angle_diff = (angle - adj) & 0xffff
            if angle_diff & 0x8000:
                angle_diff -= 0x10000
            new_angle = angle - angle_diff
            if calibration_reversed:
                new_angle = -new_angle
            samples[i] = (samp_time, new_angle)
        if self.mcu_pos_offset is None:
            self.calc_mcu_pos_offset(samples[0])
            if self.mcu_pos_offset is None:
                return None
        return self.mcu_stepper.mcu_to_commanded_position(self.mcu_pos_offset)
    def load_calibration(self, angles):
        # Calculate linear intepolation calibration buckets by solving
        # linear equations
        angle_max = 1 << ANGLE_BITS
        calibration_count = 1 << CALIBRATION_BITS
        bucket_size = angle_max // calibration_count
        full_steps = len(angles)
        nominal_step = float(angle_max) / full_steps
        self.angle_phase_offset = (angles.index(min(angles)) & 3) * nominal_step
        self.calibration_reversed = angles[-2] > angles[-1]
        if self.calibration_reversed:
            angles = list(reversed(angles))
        first_step = angles.index(min(angles))
        angles = angles[first_step:] + angles[:first_step]
        import numpy
        eqs = numpy.zeros((full_steps, calibration_count))
        ans = numpy.zeros((full_steps,))
        for step, angle in enumerate(angles):
            int_angle = int(angle + .5) % angle_max
            bucket = int(int_angle / bucket_size)
            bucket_start = bucket * bucket_size
            ang_diff = angle - bucket_start
            ang_diff_per = ang_diff / bucket_size
            eq = eqs[step]
            eq[bucket] = 1. - ang_diff_per
            eq[(bucket + 1) % calibration_count] = ang_diff_per
            ans[step] = float(step * nominal_step)
            if bucket + 1 >= calibration_count:
                ans[step] -= ang_diff_per * angle_max
        sol = numpy.linalg.lstsq(eqs, ans, rcond=None)[0]
        isol = [int(s + .5) for s in sol]
        self.calibration = isol + [isol[0] + angle_max]
    def lookup_tmc(self):
        for driver in TRINAMIC_DRIVERS:
            driver_name = "%s %s" % (driver, self.stepper_name)
            module = self.printer.lookup_object(driver_name, None)
            if module is not None:
                return module
        raise self.printer.command_error("Unable to find TMC driver for %s"
                                         % (self.stepper_name,))
    def connect(self):
        self.tmc_module = self.lookup_tmc()
        fmove = self.printer.lookup_object('force_move')
        self.mcu_stepper = fmove.lookup_stepper(self.stepper_name)
    def get_microsteps(self):
        configfile = self.printer.lookup_object('configfile')
        sconfig = configfile.get_status(None)['settings']
        stconfig = sconfig.get(self.stepper_name, {})
        microsteps = stconfig['microsteps']
        full_steps = stconfig['full_steps_per_rotation']
        return microsteps, full_steps
    def get_stepper_phase(self):
        mcu_phase_offset, phases = self.tmc_module.get_phase_offset()
        if mcu_phase_offset is None:
            raise self.printer.command_error("Driver phase not known for %s"
                                             % (self.stepper_name,))
        mcu_pos = self.mcu_stepper.get_mcu_position()
        return (mcu_pos + mcu_phase_offset) % phases
    def do_calibration_moves(self):
        move = self.printer.lookup_object('force_move').manual_move
        # Start data collection
        angle_sensor = self.printer.lookup_object(self.name)
        cconn = angle_sensor.start_internal_client()
        # Move stepper several turns (to allow internal sensor calibration)
        microsteps, full_steps = self.get_microsteps()
        mcu_stepper = self.mcu_stepper
        step_dist = mcu_stepper.get_step_dist()
        full_step_dist = step_dist * microsteps
        rotation_dist = full_steps * full_step_dist
        align_dist = step_dist * self.get_stepper_phase()
        move_time = 0.010
        move_speed = full_step_dist / move_time
        move(mcu_stepper, -(rotation_dist+align_dist), move_speed)
        move(mcu_stepper, 2. * rotation_dist, move_speed)
        move(mcu_stepper, -2. * rotation_dist, move_speed)
        move(mcu_stepper, .5 * rotation_dist - full_step_dist, move_speed)
        # Move to each full step position
        toolhead = self.printer.lookup_object('toolhead')
        times = []
        samp_dist = full_step_dist
        for i in range(2 * full_steps):
            move(mcu_stepper, samp_dist, move_speed)
            start_query_time = toolhead.get_last_move_time() + 0.050
            end_query_time = start_query_time + 0.050
            times.append((start_query_time, end_query_time))
            toolhead.dwell(0.150)
            if i == full_steps-1:
                # Reverse direction and test each full step again
                move(mcu_stepper, .5 * rotation_dist, move_speed)
                move(mcu_stepper, -.5 * rotation_dist + samp_dist, move_speed)
                samp_dist = -samp_dist
        move(mcu_stepper, .5*rotation_dist + align_dist, move_speed)
        toolhead.wait_moves()
        # Finish data collection
        cconn.finalize()
        msgs = cconn.get_messages()
        # Correlate query responses
        cal = {}
        step = 0
        for msg in msgs:
            for query_time, pos in msg['params']['data']:
                # Add to step tracking
                while step < len(times) and query_time > times[step][1]:
                    step += 1
                if step < len(times) and query_time >= times[step][0]:
                    cal.setdefault(step, []).append(pos)
        if len(cal) != len(times):
            raise self.printer.command_error(
                "Failed calibration - incomplete sensor data")
        fcal = { i: cal[i] for i in range(full_steps) }
        rcal = { full_steps-i-1: cal[i+full_steps] for i in range(full_steps) }
        return fcal, rcal
    def calc_angles(self, meas):
        total_count = total_variance = 0
        angles = {}
        for step, data in meas.items():
            count = len(data)
            angle_avg = float(sum(data)) / count
            angles[step] = angle_avg
            total_count += count
            total_variance += sum([(d - angle_avg)**2 for d in data])
        return angles, math.sqrt(total_variance / total_count), total_count
    cmd_ANGLE_CALIBRATE_help = "Calibrate angle sensor to stepper motor"
    def cmd_ANGLE_CALIBRATE(self, gcmd):
        # Perform calibration movement and capture
        old_calibration = self.calibration
        self.calibration = []
        try:
            fcal, rcal = self.do_calibration_moves()
        finally:
            self.calibration = old_calibration
        # Calculate each step position average and variance
        microsteps, full_steps = self.get_microsteps()
        fangles, fstd, ftotal = self.calc_angles(fcal)
        rangles, rstd, rtotal = self.calc_angles(rcal)
        if (len({a: i for i, a in fangles.items()}) != len(fangles)
            or len({a: i for i, a in rangles.items()}) != len(rangles)):
            raise self.printer.command_error(
                "Failed calibration - sensor not updating for each step")
        merged = { i: fcal[i] + rcal[i] for i in range(full_steps) }
        angles, std, total = self.calc_angles(merged)
        gcmd.respond_info("angle: stddev=%.3f (%.3f forward / %.3f reverse)"
                          " in %d queries" % (std, fstd, rstd, total))
        # Order data with lowest/highest magnet position first
        anglist = [angles[i] % 0xffff for i in range(full_steps)]
        if angles[0] > angles[1]:
            first_ang = max(anglist)
        else:
            first_ang = min(anglist)
        first_phase = anglist.index(first_ang) & ~3
        anglist = anglist[first_phase:] + anglist[:first_phase]
        # Save results
        cal_contents = []
        for i, angle in enumerate(anglist):
            if not i % 8:
                cal_contents.append('\n')
            cal_contents.append("%.1f" % (angle,))
            cal_contents.append(',')
        cal_contents.pop()
        configfile = self.printer.lookup_object('configfile')
        configfile.remove_section(self.name)
        configfile.set(self.name, 'calibrate', ''.join(cal_contents))

SAMPLE_PERIOD = 0.000400

class Angle:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.sample_period = config.getfloat('sample_period', SAMPLE_PERIOD,
                                             above=0.)
        self.calibration = AngleCalibration(config)
        self.last_temperature = None
        # Measurement conversion
        self.start_clock = self.time_shift = self.sample_ticks = 0
        self.last_sequence = self.last_angle = 0
        self.last_chip_mcu_clock = self.last_chip_clock = 0
        self.chip_freq = 0.
        # Measurement storage (accessed from background thread)
        self.lock = threading.Lock()
        self.raw_samples = []
        # Sensor type
        sensors = { "a1333": (3, 10000000, .000001),
                    "as5047d": (1, int(1. / .000000350), .000100),
                    "tle5012b": (1, 4000000, 0.) }
        self.sensor_type = config.getchoice('sensor_type',
                                            {s: s for s in sensors})
        spi_mode, spi_speed, self.static_delay = sensors[self.sensor_type]
        # Setup mcu sensor_spi_angle bulk query code
        self.spi = bus.MCU_SPI_from_config(config, spi_mode,
                                           default_speed=spi_speed)
        self.mcu = mcu = self.spi.get_mcu()
        self.oid = oid = mcu.create_oid()
        self.query_spi_angle_cmd = self.query_spi_angle_end_cmd = None
        self.spi_angle_transfer_cmd = None
        mcu.add_config_cmd(
            "config_spi_angle oid=%d spi_oid=%d spi_angle_type=%s"
            % (oid, self.spi.get_oid(), self.sensor_type))
        mcu.add_config_cmd(
            "query_spi_angle oid=%d clock=0 rest_ticks=0 time_shift=0"
            % (oid,), on_restart=True)
        mcu.register_config_callback(self._build_config)
        mcu.register_response(self._handle_spi_angle_data,
                              "spi_angle_data", oid)
        # API server endpoints
        self.api_dump = motion_report.APIDumpHelper(
            self.printer, self._api_update, self._api_startstop, 0.100)
        self.name = config.get_name().split()[1]
        wh = self.printer.lookup_object('webhooks')
        wh.register_mux_endpoint("angle/dump_angle", "sensor", self.name,
                                 self._handle_dump_angle)
    def _build_config(self):
        freq = self.mcu.seconds_to_clock(1.)
        while float(TCODE_ERROR << self.time_shift) / freq < 0.002:
            self.time_shift += 1
        cmdqueue = self.spi.get_command_queue()
        self.query_spi_angle_cmd = self.mcu.lookup_command(
            "query_spi_angle oid=%c clock=%u rest_ticks=%u time_shift=%c",
            cq=cmdqueue)
        self.query_spi_angle_end_cmd = self.mcu.lookup_query_command(
            "query_spi_angle oid=%c clock=%u rest_ticks=%u time_shift=%c",
            "spi_angle_end oid=%c sequence=%hu", oid=self.oid, cq=cmdqueue)
        self.spi_angle_transfer_cmd = self.mcu.lookup_query_command(
            "spi_angle_transfer oid=%c data=%*s",
            "spi_angle_transfer_response oid=%c clock=%u response=%*s",
            oid=self.oid, cq=cmdqueue)
    def get_status(self, eventtime=None):
        return {'temperature': self.last_temperature}
    # Measurement collection
    def is_measuring(self):
        return self.start_clock != 0
    def _handle_spi_angle_data(self, params):
        with self.lock:
            self.raw_samples.append(params)
    def _extract_samples(self, raw_samples):
        # Load variables to optimize inner loop below
        static_delay = self.static_delay
        sample_ticks = self.sample_ticks
        start_clock = self.start_clock
        time_shift = self.time_shift
        clock_to_print_time = self.mcu.clock_to_print_time
        last_sequence = self.last_sequence
        last_angle = self.last_angle
        is_tle5012b = self.sensor_type == "tle5012b"
        last_chip_mcu_clock = self.last_chip_mcu_clock
        chip_freq = self.chip_freq
        inv_chip_freq = 0.
        if chip_freq:
            inv_chip_freq = 1. / chip_freq
        last_chip_clock = self.last_chip_clock
        # Process every message in raw_samples
        count = error_count = 0
        samples = [None] * (len(raw_samples) * 16)
        for params in raw_samples:
            seq = (last_sequence & ~0xffff) | params['sequence']
            if seq < last_sequence:
                seq += 0x10000
            last_sequence = seq
            d = bytearray(params['data'])
            msg_mclock = start_clock + seq*16*sample_ticks
            for i in range(len(d) // 3):
                tcode = d[i*3]
                if tcode == TCODE_ERROR:
                    error_count += 1
                    continue
                raw_angle = d[i*3 + 1] | (d[i*3 + 2] << 8)
                angle_diff = (last_angle - raw_angle) & 0xffff
                if angle_diff & 0x8000:
                    angle_diff -= 0x10000
                last_angle -= angle_diff
                mclock = msg_mclock + i*sample_ticks
                if is_tle5012b:
                    # tcode is tle5012b frame counter
                    mdiff = mclock - last_chip_mcu_clock
                    chip_mclock = last_chip_clock + int(mdiff * chip_freq + .5)
                    cdiff = ((tcode << 10) - chip_mclock) & 0xffff
                    if cdiff & 0x8000:
                        cdiff -= 0x10000
                    sclock = mclock + (cdiff - 0x800) * inv_chip_freq
                else:
                    # tcode is mcu clock offset shifted by time_shift
                    sclock = mclock + (tcode<<time_shift)
                ptime = round(clock_to_print_time(sclock) - static_delay, 6)
                samples[count] = (ptime, last_angle)
                count += 1
        self.last_sequence = last_sequence
        self.last_angle = last_angle
        del samples[count:]
        return samples, error_count
    # Device specific code
    def _a1333_init(self):
        # Setup for angle query
        self.spi.spi_transfer([0x32, 0x00])
    def _as5047d_init(self):
        # Clear any errors from device
        self.spi.spi_transfer([0xff, 0xfc]) # Read DIAAGC
        self.spi.spi_transfer([0x40, 0x01]) # Read ERRFL
        self.spi.spi_transfer([0xc0, 0x00]) # Read NOP
    def _tle5012b_crc(self, data):
        crc = 0xff
        for d in data:
            crc ^= d
            for i in range(8):
                if crc & 0x80:
                    crc = (crc << 1) ^ 0x1d
                else:
                    crc <<= 1
        return (~crc) & 0xff
    def _tle5012b_query(self, msg):
        for retry in range(5):
            if msg[0] & 0x04:
                params = self.spi_angle_transfer_cmd.send([self.oid, msg])
            else:
                params = self.spi.spi_transfer(msg)
            resp = bytearray(params['response'])
            crc = self._tle5012b_crc(bytearray(msg[:2]) + resp[2:-2])
            if crc == resp[-1]:
                return params
        raise self.printer.command_error("Unable to query tle5012b chip")
    def _tle5012b_query_clock(self):
        # Read frame counter (and normalize to a 16bit counter)
        msg = [0x84, 0x42, 0, 0, 0, 0, 0, 0] # Read with latch, AREV and FSYNC
        params = self._tle5012b_query(msg)
        resp = bytearray(params['response'])
        mcu_clock = self.mcu.clock32_to_clock64(params['clock'])
        chip_clock = ((resp[2] & 0x7e) << 9) | ((resp[4] & 0x3e) << 4)
        # Calculate temperature
        temper = resp[5]
        if resp[4] & 0x01:
            temper -= 0x100
        self.last_temperature = (temper + 152) / 2.776
        return mcu_clock, chip_clock
    def _tle5012b_update_clock(self):
        mcu_clock, chip_clock = self._tle5012b_query_clock()
        mdiff = mcu_clock - self.last_chip_mcu_clock
        chip_mclock = self.last_chip_clock + int(mdiff * self.chip_freq + .5)
        cdiff = (chip_mclock - chip_clock) & 0xffff
        if cdiff & 0x8000:
            cdiff -= 0x10000
        new_chip_clock = chip_mclock - cdiff
        self.chip_freq = float(new_chip_clock - self.last_chip_clock) / mdiff
        self.last_chip_clock = new_chip_clock
        self.last_chip_mcu_clock = mcu_clock
    def _tle5012b_init(self):
        # Clear any errors from device
        self._tle5012b_query([0x80, 0x01, 0x00, 0x00, 0x00, 0x00]) # Read STAT
        # Setup starting clock values
        mcu_clock, chip_clock = self._tle5012b_query_clock()
        self.last_chip_clock = chip_clock
        self.last_chip_mcu_clock = mcu_clock
        self.chip_freq = float(1<<5) / self.mcu.seconds_to_clock(1. / 750000.)
        self._tle5012b_update_clock()
    # API interface
    def _api_update(self, eventtime):
        if self.sensor_type == "tle5012b":
            self._tle5012b_update_clock()
        with self.lock:
            raw_samples = self.raw_samples
            self.raw_samples = []
        if not raw_samples:
            return {}
        samples, error_count = self._extract_samples(raw_samples)
        if not samples:
            return {}
        offset = self.calibration.apply_calibration(samples)
        return {'data': samples, 'errors': error_count,
                'position_offset': offset}
    def _start_measurements(self):
        if self.is_measuring():
            return
        logging.info("Starting angle '%s' measurements", self.name)
        ifuncs = {'a1333': self._a1333_init, 'as5047d': self._as5047d_init,
                  'tle5012b': self._tle5012b_init}
        ifuncs[self.sensor_type]()
        # Start bulk reading
        with self.lock:
            self.raw_samples = []
        self.last_sequence = 0
        systime = self.printer.get_reactor().monotonic()
        print_time = self.mcu.estimated_print_time(systime) + MIN_MSG_TIME
        self.start_clock = reqclock = self.mcu.print_time_to_clock(print_time)
        rest_ticks = self.mcu.seconds_to_clock(self.sample_period)
        self.sample_ticks = rest_ticks
        self.query_spi_angle_cmd.send([self.oid, reqclock, rest_ticks,
                                       self.time_shift], reqclock=reqclock)
    def _finish_measurements(self):
        if not self.is_measuring():
            return
        # Halt bulk reading
        systime = self.printer.get_reactor().monotonic()
        print_time = self.mcu.estimated_print_time(systime) + MIN_MSG_TIME
        clock = self.mcu.print_time_to_clock(print_time)
        params = self.query_spi_angle_end_cmd.send([self.oid, 0, 0, 0],
                                                   minclock=clock)
        self.start_clock = 0
        with self.lock:
            self.raw_samples = []
        self.last_temperature = None
        logging.info("Stopped angle '%s' measurements", self.name)
    def _api_startstop(self, is_start):
        if is_start:
            self._start_measurements()
        else:
            self._finish_measurements()
    def _handle_dump_angle(self, web_request):
        self.api_dump.add_client(web_request)
        hdr = ('time', 'angle')
        web_request.send({'header': hdr})
    def start_internal_client(self):
        return self.api_dump.add_internal_client()

def load_config_prefix(config):
    return Angle(config)
