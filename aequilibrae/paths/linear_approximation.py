import importlib.util as iutil
import numpy as np
from typing import List, Dict
from warnings import warn
from ..utils import WorkerThread
from aequilibrae.paths.traffic_class import TrafficClass
from aequilibrae.paths.results import AssignmentResults
from aequilibrae.paths.all_or_nothing import allOrNothing
from aequilibrae import logger

try:
    from aequilibrae.paths.AoN import linear_combination, linear_combination_skims
    from aequilibrae.paths.AoN import triple_linear_combination, triple_linear_combination_skims
    from aequilibrae.paths.AoN import copy_one_dimension, copy_two_dimensions, copy_three_dimensions
except ImportError as ie:
    logger.warning(f'Could not import procedures from the binary. {ie.args}')

import scipy

if int(scipy.__version__.split('.')[1]) >= 3:
    from scipy.optimize import root_scalar

    recent_scipy = True
else:
    from scipy.optimize import root as root_scalar

    recent_scipy = False
    logger.warning("Using older version of Scipy. For better performance, use Scipy >= 1.4")

if False:
    from aequilibrae.paths.traffic_assignment import TrafficAssignment

spec = iutil.find_spec("PyQt5")
pyqt = spec is not None
if pyqt:
    from PyQt5.QtCore import pyqtSignal as SIGNAL


class LinearApproximation(WorkerThread):
    if pyqt:
        equilibration = SIGNAL(object)
        assignment = SIGNAL(object)

    def __init__(self, assig_spec, algorithm) -> None:
        WorkerThread.__init__(self, None)
        self.algorithm = algorithm
        self.rgap_target = assig_spec.rgap_target
        self.max_iter = assig_spec.max_iter
        self.cores = assig_spec.cores
        self.iteration_issue = []
        self.convergence_report = {'iteration': [],
                                   'rgap': [],
                                   'alpha': [],
                                   'warnings': []}
        if algorithm == 'bfw':
            self.convergence_report['beta0'] = []
            self.convergence_report['beta1'] = []
            self.convergence_report['beta2'] = []

        self.assig = assig_spec  # type: TrafficAssignment

        if None in [
            assig_spec.classes,
            assig_spec.vdf,
            assig_spec.capacity_field,
            assig_spec.time_field,
            assig_spec.vdf_parameters,
        ]:
            all_par = 'Traffic classes, VDF, VDF_parameters, capacity field & time_field'
            raise Exception("Parameter missing. Setting the algorithm is the last thing to do "
                            f"when assigning. Check if you have all of these: {all_par}")

        self.traffic_classes = assig_spec.classes  # type: List[TrafficClass]
        self.num_classes = len(assig_spec.classes)

        self.cap_field = assig_spec.capacity_field
        self.time_field = assig_spec.time_field
        self.vdf = assig_spec.vdf
        self.vdf_parameters = assig_spec.vdf_parameters

        self.iter = 0
        self.rgap = np.inf
        self.stepsize = 1.0
        self.conjugate_stepsize = 0.0
        self.fw_class_flow = 0
        # rgap can be a bit wiggly, specifying how many times we need to be below target rgap is a quick way to
        # ensure a better result. We might want to demand that the solution is that many consecutive times below.
        self.steps_below_needed_to_terminate = 1
        self.steps_below = 0

        # if this is one, we do not have a new direction and will get stuck. Make it 1.
        self.conjugate_direction_max = 0.99999

        # if FW stepsize is zero, we set it to the corresponding MSA stepsize and then need to not make
        # the step direction conjugate to the previous direction.
        self.do_fw_step = False
        self.conjugate_failed = False
        self.do_conjugate_step = False

        # BFW specific stuff
        self.betas = np.array([1.0, 0.0, 0.0])

        # Instantiates the arrays that we will use over and over
        self.capacity = assig_spec.capacity
        self.free_flow_tt = assig_spec.free_flow_tt
        self.fw_total_flow = assig_spec.total_flow
        self.congested_time = assig_spec.congested_time
        self.vdf_der = np.array(assig_spec.congested_time, copy=True)
        self.congested_value = np.array(assig_spec.congested_time, copy=True)

        self.step_direction = {}  # type: Dict[AssignmentResults]
        self.previous_step_direction = {}  # type: Dict[AssignmentResults]
        self.pre_previous_step_direction = {}  # type: Dict[AssignmentResults]

        for c in self.traffic_classes:
            r = AssignmentResults()
            r.prepare(c.graph, c.matrix)
            self.step_direction[c.mode] = r

        if self.algorithm in ['cfw', 'bfw']:

            for c in self.traffic_classes:
                r = AssignmentResults()
                r.prepare(c.graph, c.matrix)
                self.previous_step_direction[c.mode] = r

                r = AssignmentResults()
                r.prepare(c.graph, c.matrix)
                self.step_direction[c.mode] = r

                r = AssignmentResults()
                r.prepare(c.graph, c.matrix)
                self.pre_previous_step_direction[c.mode] = r

    def calculate_conjugate_stepsize(self):
        self.vdf.apply_derivative(
            self.vdf_der, self.fw_total_flow, self.capacity, self.free_flow_tt, *self.vdf_parameters
        )

        # TODO: This should be a sum over all supernetwork links, it's not tested for multi-class yet
        # if we can assume that all links appear in the subnetworks, then this is correct, otherwise
        # this needs more work
        numerator = 0.0
        denominator = 0.0
        for c in self.traffic_classes:
            stp_dir = self.step_direction[c.mode]
            prev_dir_minus_current_sol = np.sum(stp_dir.link_loads[:, :] - c.results.link_loads[:, :], axis=1)
            aon_minus_current_sol = np.sum(c._aon_results.link_loads[:, :] - c.results.link_loads[:, :], axis=1)
            aon_minus_prev_dir = np.sum(c._aon_results.link_loads[:, :] - stp_dir.link_loads[:, :], axis=1)
            numerator += prev_dir_minus_current_sol * aon_minus_current_sol
            denominator += prev_dir_minus_current_sol * aon_minus_prev_dir

        numerator = np.sum(numerator * self.vdf_der)
        denominator = np.sum(denominator * self.vdf_der)

        alpha = numerator / denominator
        if alpha < 0.0:
            self.stepdirection = 0.0
        elif alpha > self.conjugate_direction_max:
            self.stepdirection = self.conjugate_direction_max
        else:
            self.conjugate_stepsize = alpha

    def calculate_biconjugate_direction(self):
        self.vdf.apply_derivative(
            self.vdf_der, self.fw_total_flow, self.capacity, self.free_flow_tt, *self.vdf_parameters
        )

        # TODO: This should be a sum over all supernetwork links, it's not tested for multi-class yet
        # if we can assume that all links appear in the subnetworks, then this is correct, otherwise
        # this needs more work
        mu_numerator = 0.0
        mu_denominator = 0.0
        nu_nom = 0.0
        nu_denom = 0.0
        for c in self.traffic_classes:
            x_ = np.sum((self.step_direction[c.mode].link_loads[:, :] * self.stepsize
                         + self.previous_step_direction[c.mode].link_loads[:, :] * (1.0 - self.stepsize)
                         - c.results.link_loads[:, :]), axis=1)

            y_ = np.sum(c._aon_results.link_loads[:, :] - c.results.link_loads[:, :], axis=1)
            z_ = np.sum(self.step_direction[c.mode].link_loads[:, :] - c.results.link_loads[:, :], axis=1)
            mu_numerator += x_ * y_
            mu_denominator += x_ * np.sum(
                self.previous_step_direction[c.mode].link_loads - self.step_direction[c.mode].link_loads[:, :], axis=1)
            nu_nom += z_ * y_
            nu_denom += z_ * z_

        mu_numerator = np.sum(mu_numerator * self.vdf_der)
        mu_denominator = np.sum(mu_denominator * self.vdf_der)
        if mu_denominator == 0.0:
            mu = 0.0
        else:
            mu = -mu_numerator / mu_denominator
            mu = max(0.0, mu)

        nu_nom = np.sum(nu_nom * self.vdf_der)
        nu_denom = np.sum(nu_denom * self.vdf_der)
        if nu_denom == 0.0:
            nu = 0.0
        else:
            nu = -(nu_nom / nu_denom) + mu * self.stepsize / (1.0 - self.stepsize)
            nu = max(0.0, nu)

        self.betas[0] = 1.0 / (1.0 + nu + mu)
        self.betas[1] = nu * self.betas[0]
        self.betas[2] = mu * self.betas[0]

    def __calculate_step_direction(self):
        """Calculates step direction depending on the method"""
        sd_flows = []

        # 2nd iteration is a fw step. if the previous step replaced the aggregated
        # solution so far, we need to start anew.
        if (
                (self.iter == 2)
                or (self.stepsize == 1.0)
                or (self.do_fw_step)
                or (self.algorithm == "frank-wolfe")
                or (self.algorithm == "msa")
        ):
            # logger.info("FW step")
            self.do_fw_step = False
            self.do_conjugate_step = True
            self.conjugate_stepsize = 0.0
            for c in self.traffic_classes:
                aon_res = c._aon_results
                stp_dir_res = self.step_direction[c.mode]
                copy_two_dimensions(stp_dir_res.link_loads, aon_res.link_loads, self.cores)
                if c.results.num_skims > 0:
                    copy_three_dimensions(stp_dir_res.skims.matrix_view, aon_res.skims.matrix_view, self.cores)
                    aon_res.total_flows()
                sd_flows.append(aon_res.total_link_loads * c.pce)

        # 3rd iteration is cfw. also, if we had to reset direction search we need a cfw step before bfw
        elif (self.iter == 3) or (self.do_conjugate_step) or (self.algorithm == "cfw"):
            self.do_conjugate_step = False
            self.calculate_conjugate_stepsize()
            for c in self.traffic_classes:
                stp_dr = self.step_direction[c.mode]
                pre_previous = self.pre_previous_step_direction[c.mode]
                copy_two_dimensions(pre_previous.link_loads, stp_dr.link_loads, self.cores)
                if c.results.num_skims > 0:
                    copy_three_dimensions(pre_previous.skims.matrix_view, stp_dr.skims.matrix_view, self.cores)

                linear_combination(stp_dr.link_loads, stp_dr.link_loads,
                                   c._aon_results.link_loads, self.conjugate_stepsize, self.cores)

                if c.results.num_skims > 0:
                    linear_combination_skims(stp_dr.skims.matrix_view, stp_dr.skims.matrix_view,
                                             c._aon_results.skims.matrix_view, self.conjugate_stepsize, self.cores)

                sd_flows.append(np.sum(stp_dr.link_loads, axis=1) * c.pce)
        # biconjugate
        else:
            self.calculate_biconjugate_direction()
            # deep copy because we overwrite step_direction but need it on next iteration
            for c in self.traffic_classes:
                ppst = self.pre_previous_step_direction[c.mode]  # type: AssignmentResults
                prev_stp_dir = self.previous_step_direction[c.mode]  # type: AssignmentResults
                stp_dir = self.step_direction[c.mode]  # type: AssignmentResults

                copy_two_dimensions(ppst.link_loads, stp_dir.link_loads, self.cores)
                if c.results.num_skims > 0:
                    copy_three_dimensions(ppst.skims.matrix_view, stp_dir.skims.matrix_view, self.cores)

                triple_linear_combination(stp_dir.link_loads, c._aon_results.link_loads, stp_dir.link_loads,
                                          prev_stp_dir.link_loads, self.betas, self.cores)

                if c.results.num_skims > 0:
                    triple_linear_combination_skims(stp_dir.skims.matrix_view, c._aon_results.skims.matrix_view,
                                                    stp_dir.skims.matrix_view, prev_stp_dir.skims.matrix_view,
                                                    self.betas, self.cores)

                sd_flows.append(np.sum(stp_dir.link_loads, axis=1) * c.pce)

                copy_two_dimensions(prev_stp_dir.link_loads, ppst.link_loads, self.cores)
                if c.results.num_skims > 0:
                    copy_three_dimensions(prev_stp_dir.skims.matrix_view, ppst.skims.matrix_view, self.cores)

        self.step_direction_flow = np.sum(sd_flows, axis=0)

    def doWork(self):
        self.execute()

    def execute(self):
        for c in self.traffic_classes:
            c.graph.set_graph(self.time_field)

        logger.info(f"{self.algorithm} Assignment STATS")
        logger.info("Iteration, RelativeGap, stepsize")
        for self.iter in range(1, self.max_iter + 1):
            self.iteration_issue = []
            if pyqt:
                self.equilibration.emit(['rgap', self.rgap])
                self.equilibration.emit(['iterations', self.iter])
            flows = []
            aon_flows = []

            for c in self.traffic_classes:
                aon = allOrNothing(c.matrix, c.graph, c._aon_results)
                if pyqt:
                    aon.assignment.connect(self.signal_handler)
                aon.execute()
                c._aon_results.total_flows()
                aon_flows.append(c._aon_results.total_link_loads * c.pce)
            self.aon_total_flow = np.sum(aon_flows, axis=0)

            if self.iter == 1:
                for c in self.traffic_classes:
                    copy_two_dimensions(c.results.link_loads, c._aon_results.link_loads, self.cores)
                    c.results.total_flows()
                    copy_one_dimension(c.results.total_link_loads, c._aon_results.total_link_loads, self.cores)
                    if c.results.num_skims > 0:
                        copy_three_dimensions(c.results.skims.matrix_view, c._aon_results.skims.matrix_view, self.cores)
                    flows.append(c.results.total_link_loads * c.pce)
            else:
                self.__calculate_step_direction()
                self.calculate_stepsize()
                for c in self.traffic_classes:
                    stp_dir = self.step_direction[c.mode]
                    cls_res = c.results
                    linear_combination(cls_res.link_loads, stp_dir.link_loads, cls_res.link_loads, self.stepsize,
                                       self.cores)
                    if cls_res.num_skims > 0:
                        linear_combination_skims(cls_res.skims.matrix_view,
                                                 stp_dir.skims.matrix_view,
                                                 cls_res.skims.matrix_view,
                                                 self.stepsize,
                                                 self.cores)
                    cls_res.total_flows()
                    flows.append(cls_res.total_link_loads * c.pce)

            self.fw_total_flow = np.sum(flows, axis=0)

            # Check convergence
            # This needs to be done with the current costs, and not the future ones
            converged = False
            if self.iter > 1:
                converged = self.check_convergence()

            self.convergence_report['iteration'].append(self.iter)
            self.convergence_report['rgap'].append(self.rgap)
            self.convergence_report['warnings'].append('; '.join(self.iteration_issue))
            self.convergence_report['alpha'].append(self.stepsize)

            if self.algorithm == 'bfw':
                self.convergence_report['beta0'].append(self.betas[0])
                self.convergence_report['beta1'].append(self.betas[1])
                self.convergence_report['beta2'].append(self.betas[2])

            logger.info(f"{self.iter},{self.rgap},{self.stepsize}")
            if converged:
                if self.steps_below >= self.steps_below_needed_to_terminate:
                    break
                else:
                    self.steps_below += 1

            self.vdf.apply_vdf(
                self.congested_time, self.fw_total_flow, self.capacity, self.free_flow_tt, *self.vdf_parameters
            )

            for c in self.traffic_classes:
                c.graph.cost = self.congested_time
                if self.time_field in c.graph.skim_fields:
                    idx = c.graph.skim_fields.index(self.time_field)
                    c.graph.skims[:, idx] = self.congested_time[:]
                c._aon_results.reset()

        if self.rgap > self.rgap_target:
            logger.error(f"Desired RGap of {self.rgap_target} was NOT reached")
        logger.info(f"{self.algorithm} Assignment finished. {self.iter} iterations and {self.rgap} final gap")
        if pyqt:
            self.equilibration.emit(['rgap', self.rgap])
            self.equilibration.emit(['iterations', self.iter])
            self.equilibration.emit(['finished_threaded_procedure'])

    def calculate_stepsize(self):
        """Calculate optimal stepsize in descent direction"""
        if self.algorithm == "msa":
            self.stepsize = 1.0 / self.iter
            return

        def derivative_of_objective(stepsize):
            x = self.fw_total_flow + stepsize * (self.step_direction_flow - self.fw_total_flow)

            self.vdf.apply_vdf(self.congested_value, x, self.capacity, self.free_flow_tt, *self.vdf_parameters)
            return np.sum(self.congested_value * (self.step_direction_flow - self.fw_total_flow))

        try:
            if recent_scipy:
                min_res = root_scalar(derivative_of_objective, bracket=[0, 1])
                self.stepsize = min_res.root
                if not min_res.converged:
                    logger.warning("Descent direction stepsize finder is not converged")
            else:
                min_res = root_scalar(derivative_of_objective, 1 / self.iter)
                if not min_res.success:
                    logger.warning("Descent direction stepsize finder is not converged")
                self.stepsize = min_res.x[0]
                if self.stepsize <= 0.0 or self.stepsize >= 1.0:
                    raise ValueError('wrong root')

            self.conjugate_failed = False

        except ValueError:
            # We can have iterations where the objective function is not *strictly* convex, but the scipy method cannot deal
            # with this. Stepsize is then either given by 1 or 0, depending on where the objective function is smaller.
            # However, using zero would mean the overall solution would not get updated, and therefore we assert the stepsize
            # in order to add a small fraction of the AoN. A heuristic value equal to the corresponding MSA step size
            # seems to work well in practice.
            if self.algorithm == 'bfw':
                self.betas.fill(-1)
            if derivative_of_objective(0.0) < derivative_of_objective(1.0):
                if self.algorithm == "frank-wolfe" or self.conjugate_failed:
                    msa_step = 1.0 / self.iter
                    logger.warning(f"# Alert: Adding {msa_step} to stepsize to make it non-zero")
                    self.stepsize = msa_step
                else:
                    self.stepsize = 0.0
                    # need to reset conjugate / bi-conjugate direction search
                    self.do_fw_step = True
                    self.conjugate_failed = True
                    self.iteration_issue.append('Found bad conjugate direction step. Performing FW search')
                    # By doing it recursively, we avoid doing the same AoN again
                    self.__calculate_step_direction()
                    self.calculate_stepsize()

            else:
                # Do we want to keep some of the old solution, or just throw away everything?
                self.stepsize = 1.0

        assert 0 <= self.stepsize <= 1.0

    def check_convergence(self):
        """Calculate relative gap and return True if it is smaller than desired precision"""
        aon_cost = np.sum(self.congested_time * self.aon_total_flow)
        current_cost = np.sum(self.congested_time * self.fw_total_flow)
        self.rgap = abs(current_cost - aon_cost) / current_cost
        if self.rgap_target >= self.rgap:
            return True
        return False

    def signal_handler(self, val):
        if pyqt:
            self.assignment.emit(val)
