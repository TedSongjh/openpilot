#!/usr/bin/env python3
from cereal import car
from panda import Panda
from openpilot.common.conversions import Conversions as CV
from openpilot.common.numpy_fast import interp
from openpilot.selfdrive.car.honda.hondacan import CanBus
from openpilot.selfdrive.car.honda.values import CarControllerParams, CruiseButtons, CruiseSettings, HondaFlags, CAR, HONDA_BOSCH, \
                                                 HONDA_NIDEC_ALT_SCM_MESSAGES, HONDA_BOSCH_RADARLESS
from openpilot.selfdrive.car import create_button_events, get_safety_config
from openpilot.selfdrive.car.interfaces import CarInterfaceBase
from openpilot.selfdrive.car.disable_ecu import disable_ecu


ButtonType = car.CarState.ButtonEvent.Type
EventName = car.CarEvent.EventName
GearShifter = car.CarState.GearShifter
TransmissionType = car.CarParams.TransmissionType
BUTTONS_DICT = {CruiseButtons.RES_ACCEL: ButtonType.accelCruise, CruiseButtons.DECEL_SET: ButtonType.decelCruise,
                CruiseButtons.MAIN: ButtonType.altButton3, CruiseButtons.CANCEL: ButtonType.cancel}
SETTINGS_BUTTONS_DICT = {CruiseSettings.DISTANCE: ButtonType.gapAdjustCruise, CruiseSettings.LKAS: ButtonType.altButton1}


class CarInterface(CarInterfaceBase):
  @staticmethod
  def get_pid_accel_limits(CP, current_speed, cruise_speed):
    if CP.carFingerprint in HONDA_BOSCH:
      return CarControllerParams.BOSCH_ACCEL_MIN, CarControllerParams.BOSCH_ACCEL_MAX
    elif CP.enableGasInterceptorDEPRECATED:
      return CarControllerParams.NIDEC_ACCEL_MIN, CarControllerParams.NIDEC_ACCEL_MAX
    else:
      # NIDECs don't allow acceleration near cruise_speed,
      # so limit limits of pid to prevent windup
      ACCEL_MAX_VALS = [CarControllerParams.NIDEC_ACCEL_MAX, 0.2]
      ACCEL_MAX_BP = [cruise_speed - 2., cruise_speed - .2]
      return CarControllerParams.NIDEC_ACCEL_MIN, interp(current_speed, ACCEL_MAX_BP, ACCEL_MAX_VALS)

  @staticmethod
  def _get_params(ret, candidate, fingerprint, car_fw, experimental_long, docs):
    ret.carName = "honda"

    CAN = CanBus(ret, fingerprint)

    if candidate in HONDA_BOSCH:
      ret.safetyConfigs = [get_safety_config(car.CarParams.SafetyModel.hondaBosch)]
      ret.radarUnavailable = True
      # Disable the radar and let openpilot control longitudinal
      # WARNING: THIS DISABLES AEB!
      # If Bosch radarless, this blocks ACC messages from the camera
      ret.experimentalLongitudinalAvailable = True
      ret.openpilotLongitudinalControl = experimental_long
      ret.pcmCruise = not ret.openpilotLongitudinalControl
      ret.customStockLongAvailable = True
    else:
      ret.safetyConfigs = [get_safety_config(car.CarParams.SafetyModel.hondaNidec)]
      ret.enableGasInterceptorDEPRECATED = 0x201 in fingerprint[CAN.pt]
      ret.openpilotLongitudinalControl = True

      ret.pcmCruise = not ret.enableGasInterceptorDEPRECATED

    if candidate == CAR.HONDA_CRV_5G:
      ret.enableBsm = 0x12f8bfa7 in fingerprint[CAN.radar]

    # Detect Bosch cars with new HUD msgs
    if any(0x33DA in f for f in fingerprint.values()):
      ret.flags |= HondaFlags.BOSCH_EXT_HUD.value

    # Accord ICE 1.5T CVT has different gearbox message
    if candidate == CAR.HONDA_ACCORD and 0x191 in fingerprint[CAN.pt]:
      ret.transmissionType = TransmissionType.cvt
    # Integra has MT option
    if candidate == CAR.ACURA_INTEGRA and 0x191 not in fingerprint[CAN.pt]:
      ret.transmissionType = TransmissionType.manual

    # Certain Hondas have an extra steering sensor at the bottom of the steering rack,
    # which improves controls quality as it removes the steering column torsion from feedback.
    # Tire stiffness factor fictitiously lower if it includes the steering column torsion effect.
    # For modeling details, see p.198-200 in "The Science of Vehicle Dynamics (2014), M. Guiggiani"
    ret.lateralParams.torqueBP, ret.lateralParams.torqueV = [[0], [0]]
    ret.lateralTuning.pid.kiBP, ret.lateralTuning.pid.kpBP = [[0.], [0.]]
    ret.lateralTuning.pid.kf = 0.00006  # conservative feed-forward

    if candidate in HONDA_BOSCH:
      ret.longitudinalActuatorDelay = 0.5 # s
      if candidate in HONDA_BOSCH_RADARLESS:
        ret.stopAccel = CarControllerParams.BOSCH_ACCEL_MIN  # stock uses -4.0 m/s^2 once stopped but limited by safety model
    else:
      # default longitudinal tuning for all hondas
      ret.longitudinalTuning.kiBP = [0., 5., 35.]
      ret.longitudinalTuning.kiV = [1.2, 0.8, 0.5]

    eps_modified = False
    for fw in car_fw:
      if fw.ecu == "eps" and b"," in fw.fwVersion:
        eps_modified = True

    if candidate == CAR.HONDA_CIVIC:
      if eps_modified:
        # stock request input values:     0x0000, 0x00DE, 0x014D, 0x01EF, 0x0290, 0x0377, 0x0454, 0x0610, 0x06EE
        # stock request output values:    0x0000, 0x0917, 0x0DC5, 0x1017, 0x119F, 0x140B, 0x1680, 0x1680, 0x1680
        # modified request output values: 0x0000, 0x0917, 0x0DC5, 0x1017, 0x119F, 0x140B, 0x1680, 0x2880, 0x3180
        # stock filter output values:     0x009F, 0x0108, 0x0108, 0x0108, 0x0108, 0x0108, 0x0108, 0x0108, 0x0108
        # modified filter output values:  0x009F, 0x0108, 0x0108, 0x0108, 0x0108, 0x0108, 0x0108, 0x0400, 0x0480
        # note: max request allowed is 4096, but request is capped at 3840 in firmware, so modifications result in 2x max
        ret.lateralParams.torqueBP, ret.lateralParams.torqueV = [[0, 2560, 8000], [0, 2560, 3840]]
        ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[0.3], [0.1]]
      else:
        ret.lateralParams.torqueBP, ret.lateralParams.torqueV = [[0, 2560], [0, 2560]]
        ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[1.1], [0.33]]

    elif candidate in (CAR.HONDA_CIVIC_BOSCH, CAR.HONDA_CIVIC_BOSCH_DIESEL, CAR.HONDA_CIVIC_2022, CAR.ACURA_INTEGRA):
      if eps_modified:
        ret.lateralParams.torqueBP, ret.lateralParams.torqueV = [[0, 2564, 8000], [0, 2564, 3840]]
        ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[0.3], [0.09]]  # 2.5x Modded EPS
      else:
        ret.lateralParams.torqueBP, ret.lateralParams.torqueV = [[0, 4096], [0, 4096]]  # TODO: determine if there is a dead zone at the top end
        ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[0.8], [0.24]]

    elif candidate == CAR.HONDA_ACCORD:
      ret.lateralParams.torqueBP, ret.lateralParams.torqueV = [[0, 4096], [0, 4096]]  # TODO: determine if there is a dead zone at the top end

      if eps_modified:
        ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[0.3], [0.09]]
      else:
        ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[0.6], [0.18]]

    elif candidate == CAR.ACURA_ILX:
      ret.lateralParams.torqueBP, ret.lateralParams.torqueV = [[0, 3840], [0, 3840]]  # TODO: determine if there is a dead zone at the top end
      ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[0.8], [0.24]]

    elif candidate in (CAR.HONDA_CRV, CAR.HONDA_CRV_EU):
      ret.lateralParams.torqueBP, ret.lateralParams.torqueV = [[0, 1000], [0, 1000]]  # TODO: determine if there is a dead zone at the top end
      ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[0.8], [0.24]]
      ret.wheelSpeedFactor = 1.025

    elif candidate == CAR.HONDA_CRV_5G:
      if eps_modified:
        # stock request input values:     0x0000, 0x00DB, 0x01BB, 0x0296, 0x0377, 0x0454, 0x0532, 0x0610, 0x067F
        # stock request output values:    0x0000, 0x0500, 0x0A15, 0x0E6D, 0x1100, 0x1200, 0x129A, 0x134D, 0x1400
        # modified request output values: 0x0000, 0x0500, 0x0A15, 0x0E6D, 0x1100, 0x1200, 0x1ACD, 0x239A, 0x2800
        ret.lateralParams.torqueBP, ret.lateralParams.torqueV = [[0, 2560, 10000], [0, 2560, 3840]]
        ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[0.21], [0.07]]
      else:
        ret.lateralParams.torqueBP, ret.lateralParams.torqueV = [[0, 3840], [0, 3840]]
        ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[0.64], [0.192]]
      ret.wheelSpeedFactor = 1.025

    elif candidate == CAR.HONDA_CRV_HYBRID:
      ret.lateralParams.torqueBP, ret.lateralParams.torqueV = [[0, 4096], [0, 4096]]  # TODO: determine if there is a dead zone at the top end
      ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[0.6], [0.18]]
      ret.wheelSpeedFactor = 1.025

    elif candidate == CAR.HONDA_FIT:
      ret.lateralParams.torqueBP, ret.lateralParams.torqueV = [[0, 4096], [0, 4096]]  # TODO: determine if there is a dead zone at the top end
      ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[0.2], [0.05]]

    elif candidate == CAR.HONDA_FREED:
      ret.lateralParams.torqueBP, ret.lateralParams.torqueV = [[0, 4096], [0, 4096]]
      ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[0.2], [0.05]]

    elif candidate in (CAR.HONDA_HRV, CAR.HONDA_HRV_3G):
      ret.lateralParams.torqueBP, ret.lateralParams.torqueV = [[0, 4096], [0, 4096]]
      if candidate == CAR.HONDA_HRV:
        ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[0.16], [0.025]]
        ret.wheelSpeedFactor = 1.025
      else:
        ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[0.8], [0.24]]  # TODO: can probably use some tuning

    elif candidate == CAR.ACURA_RDX:
      ret.lateralParams.torqueBP, ret.lateralParams.torqueV = [[0, 1000], [0, 1000]]  # TODO: determine if there is a dead zone at the top end
      ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[0.8], [0.24]]

    elif candidate == CAR.ACURA_RDX_3G:
      ret.lateralParams.torqueBP, ret.lateralParams.torqueV = [[0, 3840], [0, 3840]]
      ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[0.2], [0.06]]

    elif candidate == CAR.HONDA_ACCORD_4CYL_9TH_GEN:
      ret.lateralParams.torqueBP, ret.lateralParams.torqueV = [[0, 239], [0, 239]]
      ret.lateralTuning.pid.kiBP, ret.lateralTuning.pid.kpBP = [[0.,20], [0.,20]]
      ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[0.4,0.3], [0,0]]

    elif candidate in (CAR.HONDA_ODYSSEY, CAR.HONDA_ODYSSEY_CHN):
      ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[0.28], [0.08]]
      if candidate == CAR.HONDA_ODYSSEY_CHN:
        ret.lateralParams.torqueBP, ret.lateralParams.torqueV = [[0, 32767], [0, 32767]]  # TODO: determine if there is a dead zone at the top end
      else:
        ret.lateralParams.torqueBP, ret.lateralParams.torqueV = [[0, 4096], [0, 4096]]  # TODO: determine if there is a dead zone at the top end

    elif candidate == CAR.HONDA_PILOT:
      ret.lateralParams.torqueBP, ret.lateralParams.torqueV = [[0, 4096], [0, 4096]]  # TODO: determine if there is a dead zone at the top end
      ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[0.38], [0.11]]

    elif candidate == CAR.HONDA_RIDGELINE:
      ret.lateralParams.torqueBP, ret.lateralParams.torqueV = [[0, 4096], [0, 4096]]  # TODO: determine if there is a dead zone at the top end
      ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[0.38], [0.11]]

    elif candidate == CAR.HONDA_INSIGHT:
      ret.lateralParams.torqueBP, ret.lateralParams.torqueV = [[0, 4096], [0, 4096]]  # TODO: determine if there is a dead zone at the top end
      ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[0.6], [0.18]]

    elif candidate == CAR.HONDA_E:
      ret.lateralParams.torqueBP, ret.lateralParams.torqueV = [[0, 4096], [0, 4096]]  # TODO: determine if there is a dead zone at the top end
      ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[0.6], [0.18]] # TODO: can probably use some tuning

    elif candidate == CAR.HONDA_CLARITY:
      ret.safetyConfigs[0].safetyParam |= Panda.FLAG_HONDA_CLARITY
      ret.mass = 4052. * CV.LB_TO_KG
      ret.wheelbase = 2.75
      ret.centerToFront = ret.wheelbase * 0.4
      ret.steerRatio = 16.50  # 12.72 is end-to-end spec
      if eps_modified:
        for fw in car_fw:
          if fw.ecu == "eps" and b"-" not in fw.fwVersion and b"," in fw.fwVersion:
            ret.lateralTuning.pid.kf = 0.00004
            ret.lateralParams.torqueBP, ret.lateralParams.torqueV = [[0, 0xA00, 0x3C00], [0, 2560, 3840]]
            ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[0.1575], [0.05175]]
          elif fw.ecu == "eps" and b"-" in fw.fwVersion and b"," in fw.fwVersion:
            ret.lateralParams.torqueBP, ret.lateralParams.torqueV = [[0, 0xA00, 0x2800], [0, 2560, 3840]]
            ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[0.3], [0.1]]
      else:
        ret.lateralParams.torqueBP, ret.lateralParams.torqueV = [[0, 2560], [0, 2560]]
        ret.lateralTuning.pid.kpV, ret.lateralTuning.pid.kiV = [[0.8], [0.24]]
      tire_stiffness_factor = 1.

    else:
      raise ValueError(f"unsupported car {candidate}")

    # These cars use alternate user brake msg (0x1BE)
    # TODO: Only detect feature for Accord/Accord Hybrid, not all Bosch DBCs have BRAKE_MODULE
    if 0x1BE in fingerprint[CAN.pt] and candidate in (CAR.HONDA_ACCORD, CAR.HONDA_HRV_3G):
      ret.flags |= HondaFlags.BOSCH_ALT_BRAKE.value

    if ret.flags & HondaFlags.BOSCH_ALT_BRAKE:
      ret.safetyConfigs[0].safetyParam |= Panda.FLAG_HONDA_ALT_BRAKE

    # These cars use alternate SCM messages (SCM_FEEDBACK AND SCM_BUTTON)
    if candidate in HONDA_NIDEC_ALT_SCM_MESSAGES:
      ret.safetyConfigs[0].safetyParam |= Panda.FLAG_HONDA_NIDEC_ALT

    if ret.openpilotLongitudinalControl and candidate in HONDA_BOSCH:
      ret.safetyConfigs[0].safetyParam |= Panda.FLAG_HONDA_BOSCH_LONG

    if ret.enableGasInterceptorDEPRECATED and candidate not in HONDA_BOSCH:
      ret.safetyConfigs[0].safetyParam |= Panda.FLAG_HONDA_GAS_INTERCEPTOR

    if candidate in HONDA_BOSCH_RADARLESS:
      ret.safetyConfigs[0].safetyParam |= Panda.FLAG_HONDA_RADARLESS

    # min speed to enable ACC. if car can do stop and go, then set enabling speed
    # to a negative value, so it won't matter. Otherwise, add 0.5 mph margin to not
    # conflict with PCM acc
    ret.autoResumeSng = candidate in (HONDA_BOSCH | {CAR.HONDA_CIVIC, CAR.HONDA_CLARITY}) or ret.enableGasInterceptorDEPRECATED
    ret.minEnableSpeed = -1. if ret.autoResumeSng else 25.5 * CV.MPH_TO_MS

    ret.steerActuatorDelay = 0.1
    ret.steerLimitTimer = 0.8

    return ret

  @staticmethod
  def init(CP, logcan, sendcan):
    if CP.carFingerprint in (HONDA_BOSCH - HONDA_BOSCH_RADARLESS) and CP.openpilotLongitudinalControl:
      disable_ecu(logcan, sendcan, bus=1, addr=0x18DAB0F1, com_cont_req=b'\x28\x83\x03')

  # returns a car.CarState
  def _update(self, c):
    ret = self.CS.update(self.cp, self.cp_cam, self.cp_body)

    self.CS.button_events = [
      *create_button_events(self.CS.cruise_buttons, self.CS.prev_cruise_buttons, BUTTONS_DICT),
      *create_button_events(self.CS.cruise_setting, self.CS.prev_cruise_setting, SETTINGS_BUTTONS_DICT),
    ]

    self.CS.mads_enabled = self.get_sp_cruise_main_state(ret)

    self.CS.accEnabled = self.get_sp_v_cruise_non_pcm_state(ret, c.vCruise, self.CS.accEnabled)

    if ret.cruiseState.available:
      if self.enable_mads:
        if not self.CS.prev_mads_enabled and self.CS.mads_enabled:
          self.CS.madsEnabled = True
        if any(b.type == ButtonType.altButton1 and b.pressed for b in self.CS.button_events):
          self.CS.madsEnabled = not self.CS.madsEnabled
        self.CS.madsEnabled = self.get_acc_mads(ret, self.CS.madsEnabled)
    else:
      self.CS.madsEnabled = False

    min_enable_speed_pcm = self.CP.pcmCruise and self.CP.minEnableSpeed > 0 and self.CP.pcmCruiseSpeed

    if not self.CP.pcmCruise or min_enable_speed_pcm or not self.CP.pcmCruiseSpeed:
      if any(b.type == ButtonType.cancel for b in self.CS.button_events):
        self.get_sp_cancel_cruise_state()
    if self.get_sp_pedal_disengage(ret):
      self.get_sp_cancel_cruise_state()
      ret.cruiseState.enabled = ret.cruiseState.enabled if not self.enable_mads or min_enable_speed_pcm \
                                                        else False if self.CP.pcmCruise \
                                                        else self.CS.accEnabled

    if min_enable_speed_pcm:
      if ret.gasPressed and not ret.cruiseState.enabled:
        self.CS.accEnabled = False
      if ret.cruiseState.enabled and not self.CS.out.cruiseState.enabled:
        self.CS.accEnabled = True
      elif not ret.cruiseState.enabled:
        self.CS.accEnabled = False

    ret = self.get_sp_common_state(ret)

    ret.buttonEvents = [
      *self.CS.button_events,
      *self.button_events.create_mads_event(self.CS.madsEnabled, self.CS.out.madsEnabled)  # MADS BUTTON
    ]

    # events
    events = self.create_common_events(ret, c, extra_gears=[GearShifter.sport, GearShifter.low], pcm_enable=False)
    if self.CP.pcmCruise and ret.vEgo < self.CP.minEnableSpeed and not self.CS.madsEnabled:
      events.add(EventName.belowEngageSpeed)

    events, ret = self.create_sp_events(ret, events)

    #if self.CP.pcmCruise:
    #  # we engage when pcm is active (rising edge)
    #  if ret.cruiseState.enabled and not self.CS.out.cruiseState.enabled:
    #    events.add(EventName.pcmEnable)
    #  elif not ret.cruiseState.enabled and (c.actuators.accel >= 0. or not self.CP.openpilotLongitudinalControl):
    #    # it can happen that car cruise disables while comma system is enabled: need to
    #    # keep braking if needed or if the speed is very low
    #    if ret.vEgo < self.CP.minEnableSpeed + 2.:
    #      # non loud alert if cruise disables below 25mph as expected (+ a little margin)
    #      events.add(EventName.speedTooLow)
    #    else:
    #      events.add(EventName.cruiseDisabled)
    if self.CS.CP.minEnableSpeed > 0 and ret.vEgo < 0.001:
      events.add(EventName.manualRestart)

    ret.customStockLong = self.update_custom_stock_long()

    ret.events = events.to_msg()

    return ret
