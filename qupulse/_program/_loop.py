import itertools
from typing import Union, Dict, Set, Iterable, FrozenSet, Tuple, cast, List, Optional, DefaultDict, Generator, Mapping
from collections import defaultdict, deque
from copy import deepcopy
from enum import Enum
import warnings

import numpy as np

from qupulse.utils.types import ChannelID, TimeType
from qupulse._program.instructions import AbstractInstructionBlock, EXECInstruction, REPJInstruction, GOTOInstruction,\
    STOPInstruction, CHANInstruction, Waveform, MEASInstruction, Instruction
from qupulse.utils.tree import Node, is_tree_circular
from qupulse.utils.types import MeasurementWindow
from qupulse.pulses.parameters import MappedParameter, ConstantParameter
from qupulse.expressions import ExpressionScalar

from qupulse._program.waveforms import SequenceWaveform, RepetitionWaveform

__all__ = ['Loop', 'MultiChannelProgram', 'make_compatible', 'MakeCompatibleWarning']


class Loop(Node):
    MAX_REPR_SIZE = 2000
    __slots__ = ('_waveform', '_measurements', '_repetition_count', '_cached_body_duration', '_repetition_parameter')

    """Build a loop tree. The leaves of the tree are loops with one element.
    
    Loop objects are equal if all children are/the waveform is equal, the repetition count is equal    
    """
    def __init__(self,
                 parent: Union['Loop', None] = None,
                 children: Iterable['Loop'] = (),
                 waveform: Optional[Waveform] = None,
                 measurements: Optional[List[MeasurementWindow]] = None,
                 repetition_count: int = 1,
                 repetition_parameter: MappedParameter = None):
        """Initialize a new loop

        Args:
            parent: Forwarded to Node.__init__
            children: Forwarded to Node.__init__
            waveform: "Payload"
            measurements:
            repetition_count: The children / waveform are repeated this often
            repetition_parameter: If provided, this marks the repetition count as volatile i.e. changable in the future
        """
        super().__init__(parent=parent, children=children)

        self._waveform = waveform
        self._measurements = measurements
        self._repetition_count = int(repetition_count)
        self._repetition_parameter = repetition_parameter
        self._cached_body_duration = None

        assert self._repetition_count == repetition_count, "Repetition count was not an integer: %r" % repetition_count
        assert isinstance(waveform, (type(None), Waveform))

    def __eq__(self, other: 'Loop') -> bool:
        if type(self) == type(other):
            return (self._repetition_count == other._repetition_count and
                    self.waveform == other.waveform and
                    (self._measurements or None) == (other._measurements or None) and
                    self._repetition_parameter == other._repetition_parameter and
                    len(self) == len(other) and
                    all(self_child == other_child for self_child, other_child in zip(self, other)))
        else:
            return NotImplemented

    def append_child(self, loop: Optional['Loop'] = None, **kwargs) -> None:
        """Append a child to this loop. Either an existing Loop object or a newly created from kwargs

        Args:
            loop: loop to append
            **kwargs: Child is constructed with these kwargs

        Raises:
            ValueError: if called with loop and kwargs
        """
        if loop is not None:
            if kwargs:
                raise ValueError("Cannot pass a Loop object and Loop constructor arguments at the same time in "
                                 "append_child")
            arg = (loop,)
        else:
            arg = (kwargs,)
        super().__setitem__(slice(len(self), len(self)), arg)
        self._invalidate_duration(body_duration_increment=self[-1].duration)

    def _invalidate_duration(self, body_duration_increment=None):
        if self._cached_body_duration is not None:
            if body_duration_increment is not None:
                self._cached_body_duration += body_duration_increment
            else:
                self._cached_body_duration = None
        if self.parent:
            if body_duration_increment is not None:
                self.parent._invalidate_duration(body_duration_increment=body_duration_increment*self.repetition_count)
            else:
                self.parent._invalidate_duration()

    def add_measurements(self, measurements: Iterable[MeasurementWindow]):
        """Add measurements offset by the current body duration i.e. to the END of the current loop

        Args:
            measurements: Measurements to add
        """
        body_duration = float(self.body_duration)
        if body_duration == 0:
            measurements = measurements
        else:
            measurements = ((mw_name, begin+body_duration, length) for mw_name, begin, length in measurements)

        if self._measurements is None:
            self._measurements = list(measurements)
        else:
            self._measurements.extend(measurements)

    def update_volatile_repetition(self, new_values: Mapping[str, ConstantParameter]):
        if self._repetition_parameter is not None:
            self._repetition_parameter.update_constants(new_values)
            self._repetition_count = int(self._repetition_parameter.get_value())

    @property
    def waveform(self) -> Waveform:
        return self._waveform

    @waveform.setter
    def waveform(self, val) -> None:
        self._waveform = val
        self._invalidate_duration()

    @property
    def body_duration(self) -> TimeType:
        if self._cached_body_duration is None:
            if self.is_leaf():
                if self.waveform:
                    self._cached_body_duration = self.waveform.duration
                else:
                    self._cached_body_duration = TimeType.from_fraction(0, 1)
            else:
                self._cached_body_duration = sum(child.duration for child in self)
        return self._cached_body_duration

    @property
    def duration(self) -> TimeType:
        return self.body_duration * self.repetition_count

    @property
    def repetition_parameter(self) -> Optional[MappedParameter]:
        return self._repetition_parameter

    @property
    def repetition_count(self) -> int:
        return self._repetition_count

    @repetition_count.setter
    def repetition_count(self, val) -> None:
        new_repetition = int(val)
        if abs(new_repetition - val) > 1e-10:
            raise ValueError('Repetition count was not an integer')
        self._repetition_count = new_repetition

    def unroll(self) -> None:
        if self.is_leaf():
            raise RuntimeError('Leaves cannot be unrolled')
        if self.repetition_parameter is not None:
            warnings.warn("Unrolling a Loop with volatile repetition count", VolatileModificationWarning)

        i = self.parent_index
        self.parent[i:i+1] = (child.copy_tree_structure(new_parent=self.parent)
                              for _ in range(self.repetition_count)
                              for child in self)
        self.parent.assert_tree_integrity()

    def __setitem__(self, idx, value):
        super().__setitem__(idx, value)
        self._invalidate_duration()

    def unroll_children(self) -> None:
        if self._repetition_parameter is not None:
            warnings.warn("Unrolling a Loop with volatile repetition count", VolatileModificationWarning)
        old_children = self.children
        self[:] = (child.copy_tree_structure()
                   for _ in range(self.repetition_count)
                   for child in old_children)
        self.repetition_count = 1
        self._repetition_parameter = None
        self.assert_tree_integrity()

    def encapsulate(self) -> None:
        """Add a nesting level by moving self to its children."""
        self[:] = [Loop(children=self,
                        repetition_count=self.repetition_count,
                        repetition_parameter=self._repetition_parameter,
                        waveform=self._waveform,
                        measurements=self._measurements)]
        self.repetition_count = 1
        self._repetition_parameter = None
        self._waveform = None
        self._measurements = None
        self.assert_tree_integrity()

    def _get_repr(self, first_prefix, other_prefixes) -> Generator[str, None, None]:
        if self.is_leaf():
            yield '%sEXEC %r %d times' % (first_prefix, self._waveform, self.repetition_count)
        else:
            yield '%sLOOP %d times:' % (first_prefix, self.repetition_count)

            for elem in self:
                yield from cast(Loop, elem)._get_repr(other_prefixes + '  ->', other_prefixes + '    ')

    def __repr__(self) -> str:
        is_circular = is_tree_circular(self)
        if is_circular:
            return '{}: Circ {}'.format(id(self), is_circular)

        str_len = 0
        repr_list = []
        for sub_repr in self._get_repr('', ''):
            str_len += len(sub_repr)

            if self.MAX_REPR_SIZE and str_len > self.MAX_REPR_SIZE:
                repr_list.append('...')
                break
            else:
                repr_list.append(sub_repr)
        return '\n'.join(repr_list)

    def copy_tree_structure(self, new_parent: Union['Loop', bool]=False) -> 'Loop':
        return type(self)(parent=self.parent if new_parent is False else new_parent,
                          waveform=self._waveform,
                          repetition_count=self.repetition_count,
                          repetition_parameter=self._repetition_parameter,
                          measurements=None if self._measurements is None else list(self._measurements),
                          children=(child.copy_tree_structure() for child in self))

    def _get_measurement_windows(self) -> Mapping[str, np.ndarray]:
        """Private implementation of get_measurement_windows with a slightly different data format for easier tiling.

        Returns:
             A dictionary (measurement_name -> array) with begin == array[:, 0] and length == array[:, 1]
        """
        temp_meas_windows = defaultdict(list)
        if self._measurements:
            for (mw_name, begin, length) in self._measurements:
                temp_meas_windows[mw_name].append((begin, length))

            for mw_name, begin_length_list in temp_meas_windows.items():
                temp_meas_windows[mw_name] = [np.asarray(begin_length_list, dtype=float)]

        # calculate duration together with meas windows in the same iteration
        if self.is_leaf():
            body_duration = float(self.body_duration)
        else:
            offset = TimeType(0)
            for child in self:
                for mw_name, begins_length_array in child._get_measurement_windows().items():
                    begins_length_array[:, 0] += float(offset)
                    temp_meas_windows[mw_name].append(begins_length_array)
                offset += child.duration

            body_duration = float(offset)

        # this gives us regular dict behaviour of the returned object
        temp_meas_windows.default_factory = None

        # repeat and add repetition based offset
        for mw_name, begin_length_list in temp_meas_windows.items():
            temp_begin_length_array = np.concatenate(begin_length_list)

            begin_length_array = np.tile(temp_begin_length_array, (self.repetition_count, 1))

            shaped_begin_length_array = np.reshape(begin_length_array, (self.repetition_count, -1, 2))

            shaped_begin_length_array[:, :, 0] += (np.arange(self.repetition_count) * body_duration)[:, np.newaxis]

            temp_meas_windows[mw_name] = begin_length_array

        # the cast is here because static type analysis struggles to detect that we replace _all_ values by ndarray in
        # the previous loop
        return cast(Mapping[str, np.ndarray], temp_meas_windows)

    def get_measurement_windows(self) -> Dict[str, Tuple[np.ndarray, np.ndarray]]:
        """Iterates over all children and collect the begin and length arrays of each measurement window.

        Returns:
            A dictionary (measurement_name -> (begin, length)) with begin and length being `ndarray`s
        """
        return {mw_name: (begin_length_list[:, 0], begin_length_list[:, 1])
                for mw_name, begin_length_list in self._get_measurement_windows().items()}

    def split_one_child(self, child_index=None) -> None:
        """Take the last child that has a repetition count larger one, decrease it's repetition count and insert a copy
        with repetition cout one after it"""
        if child_index is not None:
            if self[child_index].repetition_count < 2:
                raise ValueError('Cannot split child {} as the repetition count is not larger 1')

        else:
            # we cannot reverse enumerate
            n_child = len(self) - 1
            for reverse_idx, child in enumerate(reversed(self)):
                if child.repetition_count > 1:
                    forward_idx = n_child - reverse_idx
                    if child.repetition_parameter is None:
                        child_index = forward_idx
                        break
                    elif child_index is None:
                        child_index = forward_idx
            else:
                if child_index is None:
                    raise RuntimeError('There is no child with repetition count > 1')

        if self[child_index]._repetition_parameter is not None:
            warnings.warn("Splitting a child with volatile repetition count", VolatileModificationWarning)
            self[child_index]._repetition_parameter = MappedParameter(expression=self[child_index]._repetition_parameter.expression - 1,
                                                                      namespace=self[child_index]._repetition_parameter.dependencies)

        new_child = self[child_index].copy_tree_structure()
        new_child.repetition_count = 1
        new_child._repetition_parameter = None

        self[child_index].repetition_count -= 1

        self[child_index+1:child_index+1] = (new_child,)
        self.assert_tree_integrity()

    def flatten_and_balance(self, depth: int) -> None:
        """Modifies the program so all tree branches have the same depth.

        Args:
            depth: Target depth of the program
        """
        i = 0
        while i < len(self):
            # only used by type checker
            sub_program = cast(Loop, self[i])

            if sub_program.depth() < depth - 1:
                # increase nesting because the subprogram is not deep enough
                sub_program.encapsulate()

            elif not sub_program.is_balanced():
                # balance the sub program. We revisit it in the next iteration (no change of i )
                # because it might modify self. While writing this comment I am not sure this is true. 14.01.2020 Simon
                sub_program.flatten_and_balance(depth - 1)

            elif sub_program.depth() == depth - 1:
                # subprogram is balanced with the correct depth
                i += 1

            elif sub_program._has_single_child_that_can_be_merged():
                # subprogram is balanced but to deep and has no measurements -> we can "lift" the sub-sub-program
                # TODO: There was a len(sub_sub_program) == 1 check here that I cannot explain
                sub_program._merge_single_child()

            elif not sub_program.is_leaf():
                # subprogram is balanced but too deep
                sub_program.unroll()

            else:
                # we land in this case if the function gets called with depth == 0 and the current subprogram is a leaf
                i += 1

    def _has_single_child_that_can_be_merged(self) -> bool:
        if len(self) == 1:
            child = cast(Loop, self[0])
            return not self._measurements or (child.repetition_count == 1 and child.repetition_parameter is None)
        else:
            return False

    def _merge_single_child(self):
        """Lift the single child to current level. Requires _has_single_child_that_can_be_merged to be true"""
        assert len(self) == 1, "bug: _merge_single_child called on loop with len != 1"
        child = cast(Loop, self[0])

        # if the child has a fixed repetition count of 1 the measurements can be merged
        mergable_measurements = child.repetition_count == 1 and child.repetition_parameter is None

        assert not self._measurements or mergable_measurements, "bug: _merge_single_child called on loop with measurements"
        assert not self._waveform, "bug: _merge_single_child called on loop with children and waveform"

        measurements = child._measurements
        if self._measurements:
            if measurements:
                measurements.extend(self._measurements)
            else:
                measurements = self._measurements

        repetition_count = self.repetition_count * child.repetition_count

        if self._repetition_parameter is None and child._repetition_parameter is None:
            repetition_parameter = None
        elif self._repetition_parameter is None:
            repetition_parameter = MappedParameter(
                expression=child._repetition_parameter.expression * self.repetition_count,
                namespace=child._repetition_parameter._namespace)
        elif child._repetition_parameter is None:
            repetition_parameter = MappedParameter(
                expression=self._repetition_parameter.expression * child.repetition_count,
                namespace=self._repetition_parameter._namespace)
        else:
            # create a new expression that depends on both
            expression = ExpressionScalar('parent_repetition_count * child_repetition_count')
            namespace = dict(parent_repetition_count=self.repetition_parameter,
                             child_repetition_count=child.repetition_parameter)
            repetition_parameter = MappedParameter(expression=expression,
                                                   namespace=namespace)

        self[:] = iter(child)
        self._waveform = child._waveform
        self._repetition_parameter = repetition_parameter
        self._repetition_count = repetition_count
        self._measurements = measurements
        self._invalidate_duration()
        return True

    def cleanup(self, actions=('remove_empty_loops', 'merge_single_child')):
        """Apply the specified actions to cleanup the Loop.

        remove_empty_loops: Remove loops with no children and no waveform (a DroppedMeasurementWarning is issued)
        merge_single_child: see `_try_merge_single_child` documentation

        Warnings:
            DroppedMeasurementWarning: Likely a bug in qupulse. TODO: investigate whether there are usecases
        """
        if 'remove_empty_loops' in actions:
            new_children = []
            for child in self:
                child = cast(Loop, child)
                if child.is_leaf():
                    if child.waveform is None:
                        if child._measurements:
                            warnings.warn("Dropping measurement since there is no waveform attached",
                                          category=DroppedMeasurementWarning)
                    else:
                        new_children.append(child)

                else:
                    child.cleanup(actions)
                    if child.waveform or not child.is_leaf():
                        new_children.append(child)

                    elif child._measurements:
                        warnings.warn("Dropping measurement since there is no waveform in children",
                                      category=DroppedMeasurementWarning)

            if len(self) != len(new_children):
                self[:] = new_children

        else:
            # only do the recursive call
            for child in self:
                child.cleanup(actions)

        if 'merge_single_child' in actions and self._has_single_child_that_can_be_merged():
            self._merge_single_child()
    
    def get_duration_structure(self) -> Tuple[int, Union[TimeType, tuple]]:
        if self.is_leaf():
            return self.repetition_count, self.waveform.duration
        else:
            return self.repetition_count, tuple(child.get_duration_structure() for child in self)


class ChannelSplit(Exception):
    def __init__(self, channel_sets):
        self.channel_sets = channel_sets


class MultiChannelProgram:
    def __init__(self, instruction_block: Union[AbstractInstructionBlock, Loop], channels: Iterable[ChannelID] = None):
        """Channels with identifier None are ignored."""
        self._programs = dict()
        if isinstance(instruction_block, AbstractInstructionBlock):
            self._init_from_instruction_block(instruction_block, channels)
        elif isinstance(instruction_block, Loop):
            assert channels is None
            self._init_from_loop(loop=instruction_block)
        else:
            raise TypeError('Invalid program type', type(instruction_block), instruction_block)

        for program in self.programs.values():
            program.cleanup()

    def _init_from_loop(self, loop: Loop):
        first_waveform = next(loop.get_depth_first_iterator()).waveform

        assert first_waveform is not None

        self._programs[frozenset(first_waveform.defined_channels)] = loop

    def _init_from_instruction_block(self, instruction_block, channels):
        if channels is None:
            def find_defined_channels(instruction_list):
                for instruction in instruction_list:
                    if isinstance(instruction, EXECInstruction):
                        yield instruction.waveform.defined_channels
                    elif isinstance(instruction, REPJInstruction):
                        yield from find_defined_channels(
                            instruction.target.block.instructions[instruction.target.offset:])
                    elif isinstance(instruction, GOTOInstruction):
                        yield from find_defined_channels(instruction.target.block.instructions[instruction.target.offset:])
                    elif isinstance(instruction, CHANInstruction):
                        yield itertools.chain(*instruction.channel_to_instruction_block.keys())
                    elif isinstance(instruction, STOPInstruction):
                        return
                    elif isinstance(instruction, MEASInstruction):
                        pass
                    else:
                        raise TypeError('Unhandled instruction type', type(instruction))

            try:
                channels = next(find_defined_channels(instruction_block.instructions))
            except StopIteration:
                raise ValueError('Instruction block has no defined channels')
        else:
            channels = set(channels)

        channels = frozenset(channels - {None})

        root = Loop()
        stacks = {channels: (root, [((), deque(instruction_block.instructions))])}

        while len(stacks) > 0:
            chans, (root_loop, stack) = stacks.popitem()
            try:
                self._programs[chans] = MultiChannelProgram.__split_channels(chans, root_loop, stack)
            except ChannelSplit as split:
                for new_channel_set in split.channel_sets:
                    assert (new_channel_set not in stacks)
                    assert (chans.issuperset(new_channel_set))

                    stacks[new_channel_set] = (root_loop.copy_tree_structure(), deepcopy(stack))

    @property
    def programs(self) -> Dict[FrozenSet[ChannelID], Loop]:
        return self._programs

    @property
    def channels(self) -> Set[ChannelID]:
        return set(itertools.chain(*self._programs.keys()))

    @staticmethod
    def __split_channels(channels: FrozenSet[ChannelID],
                         root_loop: Loop,
                         block_stack: List[Tuple[Tuple[int, ...],
                                                 deque]]) -> Loop:
        while block_stack:
            current_loop_location, current_instruction_block = block_stack.pop()
            current_loop = root_loop.locate(current_loop_location)

            while current_instruction_block:
                instruction = current_instruction_block.popleft()

                if isinstance(instruction, EXECInstruction):
                    if not instruction.waveform.defined_channels.issuperset(channels):
                        raise Exception(instruction.waveform.defined_channels, channels)
                    current_loop.append_child(waveform=instruction.waveform)

                elif isinstance(instruction, REPJInstruction):
                    if current_instruction_block:
                        block_stack.append((current_loop_location, current_instruction_block))

                    current_loop.append_child(repetition_count=instruction.count)
                    block_stack.append(
                        (current_loop[-1].get_location(),
                         deque(instruction.target.block[instruction.target.offset:-1]))
                    )
                    break

                elif isinstance(instruction, CHANInstruction):
                    if channels in instruction.channel_to_instruction_block.keys():
                        # push to front
                        new_instruction_ptr = instruction.channel_to_instruction_block[channels]
                        new_instruction_list = [*new_instruction_ptr.block[new_instruction_ptr.offset:-1]]
                        current_instruction_block.extendleft(new_instruction_list)

                    else:
                        block_stack.append((current_loop_location, deque([instruction]) + current_instruction_block))

                        raise ChannelSplit(instruction.channel_to_instruction_block.keys())

                elif isinstance(instruction, MEASInstruction):
                    current_loop.add_measurements(instruction.measurements)

                else:
                    raise Exception('Encountered unhandled instruction {} on channel(s) {}'.format(instruction, channels))
        return root_loop

    def __getitem__(self, item: Union[ChannelID, Set[ChannelID], FrozenSet[ChannelID]]) -> Loop:
        if not isinstance(item, (set, frozenset)):
            item = frozenset((item,))
        elif isinstance(item, set):
            item = frozenset(item)

        for channels, program in self._programs.items():
            if item.issubset(channels):
                return program
        raise KeyError(item)


def to_waveform(program: Loop) -> Waveform:
    if program.is_leaf():
        if program.repetition_count == 1:
            return program.waveform
        else:
            return RepetitionWaveform(program.waveform, program.repetition_count)
    else:
        if len(program) == 1:
            sequenced_waveform = to_waveform(cast(Loop, program[0]))
        else:
            sequenced_waveform = SequenceWaveform([to_waveform(cast(Loop, sub_program))
                                                   for sub_program in program])
        if program.repetition_count > 1:
            return RepetitionWaveform(sequenced_waveform, program.repetition_count)
        else:
            return sequenced_waveform


class _CompatibilityLevel(Enum):
    compatible = 0
    action_required = 1
    incompatible_too_short = 2
    incompatible_fraction = 3
    incompatible_quantum = 4

    def is_incompatible(self) -> bool:
        return self in (self.incompatible_fraction, self.incompatible_quantum, self.incompatible_too_short)


def _is_compatible(program: Loop, min_len: int, quantum: int, sample_rate: TimeType) -> _CompatibilityLevel:
    """ check whether program loop is compatible with awg requirements
        possible reasons for incompatibility:
            program shorter than minimum length
            program duration not an integer
            program duration not a multiple of quantum """
    program_duration_in_samples = program.duration * sample_rate

    if program_duration_in_samples.denominator != 1:
        return _CompatibilityLevel.incompatible_fraction

    if program_duration_in_samples < min_len:
        return _CompatibilityLevel.incompatible_too_short

    if program_duration_in_samples % quantum > 0:
        return _CompatibilityLevel.incompatible_quantum

    if program.is_leaf():
        waveform_duration_in_samples = program.body_duration * sample_rate
        if waveform_duration_in_samples < min_len or (waveform_duration_in_samples / quantum).denominator != 1:
            if program.repetition_parameter is not None:
                warnings.warn("_is_compatible requires an action which drops volatility.",
                              category=VolatileModificationWarning)
            return _CompatibilityLevel.action_required
        else:
            return _CompatibilityLevel.compatible
    else:
        if all(_is_compatible(cast(Loop, sub_program), min_len, quantum, sample_rate) == _CompatibilityLevel.compatible
               for sub_program in program):
            return _CompatibilityLevel.compatible
        else:
            if program.repetition_parameter is not None:
                warnings.warn("_is_compatible requires an action which drops volatility.",
                              category=VolatileModificationWarning)
            return _CompatibilityLevel.action_required


def _make_compatible(program: Loop, min_len: int, quantum: int, sample_rate: TimeType) -> None:
    if program.is_leaf():
        program.waveform = to_waveform(program.copy_tree_structure())
        program.repetition_count = 1
        program._repetition_parameter = None
    else:
        comp_levels = [_is_compatible(cast(Loop, sub_program), min_len, quantum, sample_rate)
                       for sub_program in program]

        if any(comp_level.is_incompatible() for comp_level in comp_levels):
            single_run = program.duration * sample_rate / program.repetition_count
            if (single_run / quantum).denominator == 1 and single_run >= min_len:
                # it is enough to concatenate all children
                new_repetition_count = program.repetition_count
                new_repetition_parameter = program.repetition_parameter
                program.repetition_count = 1
            else:
                # we need to concatenate all children and unroll
                new_repetition_count = 1
                new_repetition_parameter = None
            program.waveform = to_waveform(program.copy_tree_structure())
            program.repetition_count = new_repetition_count
            program._repetition_parameter = new_repetition_parameter
            program[:] = []
            return
        else:
            for sub_program, comp_level in zip(program, comp_levels):
                if comp_level == _CompatibilityLevel.action_required:
                    _make_compatible(sub_program, min_len, quantum, sample_rate)


def make_compatible(program: Loop, minimal_waveform_length: int, waveform_quantum: int, sample_rate: TimeType):
    """ check program for compatibility to AWG requirements, make it compatible if necessary and  possible"""
    comp_level = _is_compatible(program,
                                min_len=minimal_waveform_length,
                                quantum=waveform_quantum,
                                sample_rate=sample_rate)
    if comp_level == _CompatibilityLevel.incompatible_fraction:
        raise ValueError('The program duration in samples {} is not an integer'.format(program.duration * sample_rate))
    if comp_level == _CompatibilityLevel.incompatible_too_short:
        raise ValueError('The program is too short to be a valid waveform. \n'
                         ' program duration in samples: {} \n'
                         ' minimal length: {}'.format(program.duration * sample_rate, minimal_waveform_length))
    if comp_level == _CompatibilityLevel.incompatible_quantum:
        raise ValueError('The program duration in samples {} '
                         'is not a multiple of quantum {}'.format(program.duration * sample_rate, waveform_quantum))

    elif comp_level == _CompatibilityLevel.action_required:
        warnings.warn("qupulse will now concatenate waveforms to make the pulse/program compatible with the chosen AWG."
                      " This might take some time. If you need this pulse more often it makes sense to write it in a "
                      "way which is more AWG friendly.", MakeCompatibleWarning)

        _make_compatible(program,
                         min_len=minimal_waveform_length,
                         quantum=waveform_quantum,
                         sample_rate=sample_rate)

    else:
        assert comp_level == _CompatibilityLevel.compatible


class MakeCompatibleWarning(ResourceWarning):
    pass


class VolatileModificationWarning(RuntimeWarning):
    """This warning is emitted if the colatile part of a program gets modified. This might imply that the volatile
    parameter cannot be change anymore."""


class DroppedMeasurementWarning(RuntimeWarning):
    """This warning is emitted if a measurement was dropped because there was no waveform attached."""
