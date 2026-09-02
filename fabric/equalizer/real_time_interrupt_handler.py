import torch
import logging
import time
import asyncio
import threading
import heapq
import numpy as np
from typing import Dict, Any, List, Tuple, Optional, Set, Callable, Union, Deque
from dataclasses import dataclass, field
from enum import Enum, auto
from collections import deque, defaultdict
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("gpt4o.real_time_interrupt")

class InterruptError(Exception):
    """Base exception for interrupt handling errors."""
    pass

class InterruptTimeout(InterruptError):
    """Raised when an interrupt operation times out."""
    pass

class InterruptPriorityError(InterruptError):
    """Raised when there is a priority conflict."""
    pass

class InterruptType(Enum):
    """Types of interrupts that can be processed."""
    NEW_INPUT = auto()           
    INPUT_END = auto()           
    OUTPUT_REQUEST = auto()      
    ABORT = auto()               
    CONTEXT_SWITCH = auto()      
    MEMORY_PRESSURE = auto()     
    SAFETY_ALERT = auto()        
    EXTERNAL = auto()            
    HEARTBEAT = auto()           
    MODALITY_SYNC = auto()       

class InterruptPriority(Enum):
    """Priority levels for interrupt handling."""
    CRITICAL = 0    
    HIGH = 1        
    MEDIUM = 2      
    LOW = 3         
    BACKGROUND = 4  

@dataclass(order=True)
class InterruptRequest:
    """A queued interrupt with metadata."""
    priority: InterruptPriority
    timestamp: float
    type: InterruptType = field(compare=False)
    source_modality: Optional[str] = field(default=None, compare=False)
    target_modality: Optional[str] = field(default=None, compare=False)
    data: Dict[str, Any] = field(default_factory=dict, compare=False)
    context_id: str = field(default="default", compare=False)
    sequence_id: int = field(default=0, compare=False)
    callback: Optional[Callable] = field(default=None, compare=False)
    handled: bool = field(default=False, compare=False)
    response: Dict[str, Any] = field(default_factory=dict, compare=False)
    interrupt_id: str = field(default_factory=lambda: f"{time.time_ns()}", compare=False)

class ModelState(Enum):
    """Current state of the model processing."""
    IDLE = auto()           # Not actively processing
    PROCESSING = auto()     # Processing input
    GENERATING = auto()     # Generating output
    INTERRUPTED = auto()    # Currently handling an interrupt
    ERROR = auto()          # In error state
    SWITCHING = auto()      # Switching contexts

@dataclass
class GenerationContext:
    """Context for generation that can be interrupted and resumed."""
    context_id: str
    sequence_id: int = 0
    kv_cache: Optional[Dict[str, torch.Tensor]] = None
    attention_mask: Optional[torch.Tensor] = None
    position_ids: Optional[torch.Tensor] = None
    last_token_indices: Optional[List[int]] = None
    modality_states: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
    is_suspended: bool = False
    
    def suspend(self) -> None:
        """Suspend this context, recording state for later resumption."""
        self.is_suspended = True
    
    def resume(self) -> None:
        """Resume a suspended context."""
        self.is_suspended = False
        self.timestamp = time.time()
    
    def update(self, **kwargs) -> None:
        """Update context with new data."""
        for key, value in kwargs.items():
            setattr(self, key, value)

@dataclass
class TokenCallbackState:
    """State to track callbacks requested for specific generated tokens."""
    token_idx: int
    callback: Callable
    context_id: str
    extra_data: Dict[str, Any] = field(default_factory=dict)

class RealTimeInterruptTimings:
    """Timing metrics for interrupt handling."""
    def __init__(self):
        self.interrupt_latencies = []  # in milliseconds
        self.handling_times = []       # in milliseconds
        self.recovery_times = []       # in milliseconds
        self.context_switch_times = [] # in milliseconds
    
    def add_latency(self, latency_ms: float) -> None:
        """Add a new interrupt latency measurement."""
        self.interrupt_latencies.append(latency_ms)
        if len(self.interrupt_latencies) > 1000:
            self.interrupt_latencies.pop(0)
    
    def add_handling_time(self, time_ms: float) -> None:
        """Add a new handling time measurement."""
        self.handling_times.append(time_ms)
        if len(self.handling_times) > 1000:
            self.handling_times.pop(0)
    
    def add_recovery_time(self, time_ms: float) -> None:
        """Add a new recovery time measurement."""
        self.recovery_times.append(time_ms)
        if len(self.recovery_times) > 1000:
            self.recovery_times.pop(0)
    
    def add_context_switch_time(self, time_ms: float) -> None:
        """Add a new context switch time measurement."""
        self.context_switch_times.append(time_ms)
        if len(self.context_switch_times) > 1000:
            self.context_switch_times.pop(0)
    
    def get_stats(self) -> Dict[str, Dict[str, float]]:
        """Get statistics about interrupt timing performance."""
        stats = {}
        for name, times in [
            ("latency", self.interrupt_latencies),
            ("handling", self.handling_times),
            ("recovery", self.recovery_times),
            ("context_switch", self.context_switch_times),
        ]:
            if not times:
                stats[name] = {"mean": 0, "median": 0, "p95": 0, "p99": 0, "max": 0}
                continue
            
            times_array = np.array(times)
            stats[name] = {
                "mean": float(np.mean(times_array)),
                "median": float(np.median(times_array)),
                "p95": float(np.percentile(times_array, 95)),
                "p99": float(np.percentile(times_array, 99)),
                "max": float(np.max(times_array))
            }
        
        return stats

class InterruptHandlerMetrics:
    """Performance metrics for the interrupt handler."""
    def __init__(self):
        self.interrupts_received = 0
        self.interrupts_handled = 0
        self.interrupts_dropped = 0
        self.interrupts_by_type = defaultdict(int)
        self.interrupts_by_priority = defaultdict(int)
        self.context_switches = 0
        self.aborted_generations = 0
        self.timings = RealTimeInterruptTimings()
    
    def record_interrupt(self, interrupt: InterruptRequest) -> None:
        """Record a new interrupt."""
        self.interrupts_received += 1
        self.interrupts_by_type[interrupt.type.name] += 1
        self.interrupts_by_priority[interrupt.priority.name] += 1
    
    def record_handled(self) -> None:
        """Record an interrupt was successfully handled."""
        self.interrupts_handled += 1
    
    def record_dropped(self) -> None:
        """Record an interrupt was dropped."""
        self.interrupts_dropped += 1
    
    def record_context_switch(self) -> None:
        """Record a context switch occurred."""
        self.context_switches += 1
    
    def record_abort(self) -> None:
        """Record a generation was aborted."""
        self.aborted_generations += 1
    
    def get_summary(self) -> Dict[str, Any]:
        """Get a summary of all metrics."""
        return {
            "total": {
                "received": self.interrupts_received,
                "handled": self.interrupts_handled,
                "dropped": self.interrupts_dropped,
                "success_rate": (self.interrupts_handled / self.interrupts_received) 
                               if self.interrupts_received else 0,
            },
            "by_type": dict(self.interrupts_by_type),
            "by_priority": dict(self.interrupts_by_priority),
            "context_switches": self.context_switches,
            "aborted_generations": self.aborted_generations,
            "timing_stats": self.timings.get_stats(),
        }

class InterruptHandler:
    """
    Real-time interrupt handler for the GPT-4o framework.
    
    Manages input/output concurrency and allows for immediate response
    to new stimuli during output generation.
    
    Features:
    - Priority-based interrupt queuing
    - Context switching with KV cache preservation
    - Token rollback for generation interruption
    - Modality handoff detection
    - Non-blocking interrupt handling
    """
    
    def __init__(self, 
                 max_contexts: int = 5, 
                 max_kv_cache_size: int = 8192,
                 handle_interval_ms: int = 10, 
                 heartbeat_interval_ms: int = 250,
                 thread_pool_size: int = 4):
        """
        Initialize the interrupt handler.
        
        Args:
            max_contexts: Maximum number of generation contexts to keep in memory
            max_kv_cache_size: Maximum number of tokens in KV cache per context
            handle_interval_ms: How often to check for new interrupts (ms)
            heartbeat_interval_ms: Interval between heartbeat interrupts (ms)
            thread_pool_size: Number of threads for async interrupt handling
        """
        # Core state
        self.interrupt_queue: List[InterruptRequest] = []
        self.contexts: Dict[str, GenerationContext] = {}
        self.active_context_id: Optional[str] = None
        self.current_state = ModelState.IDLE
        self.token_callbacks: Dict[str, List[TokenCallbackState]] = defaultdict(list)
        
        # Settings
        self.max_contexts = max_contexts
        self.max_kv_cache_size = max_kv_cache_size
        self.handle_interval_ms = handle_interval_ms
        self.heartbeat_interval_ms = heartbeat_interval_ms
        
        # Thread management
        self._lock = threading.RLock()
        self.handler_thread = None
        self.heartbeat_thread = None
        self.is_running = False
        self.thread_pool = ThreadPoolExecutor(max_workers=thread_pool_size)
        
        # Performance tracking
        self.last_interrupt_time = 0
        self.metrics = InterruptHandlerMetrics()
        
        # Default handler functions
        self.default_handlers = {
            InterruptType.NEW_INPUT: self._handle_new_input,
            InterruptType.INPUT_END: self._handle_input_end,
            InterruptType.OUTPUT_REQUEST: self._handle_output_request,
            InterruptType.ABORT: self._handle_abort,
            InterruptType.CONTEXT_SWITCH: self._handle_context_switch,
            InterruptType.MEMORY_PRESSURE: self._handle_memory_pressure,
            InterruptType.SAFETY_ALERT: self._handle_safety_alert,
            InterruptType.EXTERNAL: self._handle_external,
            InterruptType.HEARTBEAT: self._handle_heartbeat,
            InterruptType.MODALITY_SYNC: self._handle_modality_sync,
        }
        
        # Custom handler registry
        self.custom_handlers: Dict[InterruptType, List[Callable]] = defaultdict(list)
        
        logger.info("Interrupt handler initialized.")

    def start(self) -> None:
        """Start the interrupt handler and its background threads."""
        with self._lock:
            if self.is_running:
                logger.warning("Interrupt handler is already running.")
                return
            
            self.is_running = True
            
            # Start handler thread
            self.handler_thread = threading.Thread(
                target=self._handler_loop,
                daemon=True,
                name="InterruptHandler"
            )
            self.handler_thread.start()
            
            # Start heartbeat thread
            self.heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop,
                daemon=True,
                name="InterruptHeartbeat"
            )
            self.heartbeat_thread.start()
            
            logger.info("Interrupt handler started.")

    def stop(self) -> None:
        """Stop the interrupt handler and clean up resources."""
        with self._lock:
            if not self.is_running:
                logger.warning("Interrupt handler is not running.")
                return
            
            self.is_running = False
            
            # Wait for threads to terminate
            if self.handler_thread and self.handler_thread.is_alive():
                self.handler_thread.join(timeout=1.0)
            
            if self.heartbeat_thread and self.heartbeat_thread.is_alive():
                self.heartbeat_thread.join(timeout=1.0)
            
            # Clean up thread pool
            self.thread_pool.shutdown(wait=False)
            
            logger.info("Interrupt handler stopped.")

    def add_interrupt(self, 
                     interrupt_type: Union[InterruptType, str],
                     data: Dict[str, Any],
                     priority: Union[InterruptPriority, str, int] = InterruptPriority.MEDIUM,
                     source_modality: Optional[str] = None,
                     target_modality: Optional[str] = None,
                     context_id: Optional[str] = None,
                     callback: Optional[Callable] = None) -> str:
        """
        Add a new interrupt to the handler queue.
        
        Args:
            interrupt_type: Type of interrupt
            data: Additional data for the interrupt
            priority: Priority level for handling
            source_modality: Which modality triggered the interrupt
            target_modality: Which modality the interrupt affects
            context_id: Which generation context this applies to
            callback: Optional function to call after handling
            
        Returns:
            interrupt_id: Unique ID for the queued interrupt
        """
        with self._lock:
            # Convert string types to enums if needed
            if isinstance(interrupt_type, str):
                interrupt_type = InterruptType[interrupt_type]
                
            if isinstance(priority, str):
                priority = InterruptPriority[priority]
            elif isinstance(priority, int):
                priority = InterruptPriority(priority)
            
            # Set context ID to current if not provided
            if context_id is None:
                context_id = self.active_context_id or "default"
                
            # Create and queue the interrupt request
            interrupt = InterruptRequest(
                priority=priority,
                timestamp=time.time(),
                type=interrupt_type,
                source_modality=source_modality,
                target_modality=target_modality,
                data=data or {},
                context_id=context_id,
                sequence_id=0,  # Will be set in _queue_interrupt
                callback=callback
            )
            
            self._queue_interrupt(interrupt)
            self.metrics.record_interrupt(interrupt)
            
            logger.info(f"Added {priority.name} interrupt: {interrupt_type.name} for context {context_id}")
            return interrupt.interrupt_id

    def register_handler(self, 
                       interrupt_type: InterruptType,
                       handler_func: Callable[[InterruptRequest], None]) -> None:
        """
        Register a custom handler for specific interrupt types.
        
        Args:
            interrupt_type: The type of interrupt to handle
            handler_func: Function to call when handling this type
        """
        with self._lock:
            self.custom_handlers[interrupt_type].append(handler_func)
            logger.info(f"Registered custom handler for {interrupt_type.name}")

    def process_interrupts(self, max_count: Optional[int] = None, timeout_ms: Optional[int] = None) -> int:
        """
        Process pending interrupts synchronously.
        
        Args:
            max_count: Maximum number of interrupts to process
            timeout_ms: Maximum time to spend processing (ms)
            
        Returns:
            Number of interrupts processed
        """
        processed_count = 0
        start_time = time.time()
        
        with self._lock:
            # Make a copy of the queue to avoid issues with modification
            interrupts = list(self.interrupt_queue)
            
        # Process up to max_count interrupts
        for interrupt in interrupts:
            if max_count is not None and processed_count >= max_count:
                break
                
            # Check timeout
            if timeout_ms is not None and (time.time() - start_time) * 1000 >= timeout_ms:
                break
                
            # Process the interrupt
            self._process_single_interrupt(interrupt)
            processed_count += 1
            
            # Remove from queue if successful
            with self._lock:
                if interrupt in self.interrupt_queue:
                    self.interrupt_queue.remove(interrupt)
        
        return processed_count

    def create_context(self, context_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """
        Create a new generation context.
        
        Args:
            context_id: Unique identifier for the context
            metadata: Additional information about the context
        """
        with self._lock:
            if context_id in self.contexts:
                logger.warning(f"Context {context_id} already exists. Updating metadata.")
                self.contexts[context_id].metadata.update(metadata or {})
                return
                
            # If we're at capacity, remove oldest inactive context
            if len(self.contexts) >= self.max_contexts:
                inactive_contexts = [
                    ctx_id for ctx_id, ctx in self.contexts.items() 
                    if ctx_id != self.active_context_id and ctx.is_suspended
                ]
                
                if inactive_contexts:
                    oldest_context = min(
                        inactive_contexts,
                        key=lambda ctx_id: self.contexts[ctx_id].timestamp
                    )
                    del self.contexts[oldest_context]
                    logger.info(f"Removed inactive context {oldest_context} to make room")
                else:
                    logger.warning(
                        f"Could not create context {context_id}: all contexts active. "
                        f"Increase max_contexts or manually remove a context."
                    )
                    return
            
            # Create the new context
            self.contexts[context_id] = GenerationContext(
                context_id=context_id,
                metadata=metadata or {}
            )
            
            logger.info(f"Created new context {context_id}")

    def switch_context(self, context_id: str) -> bool:
        """
        Switch to a different generation context.
        
        Args:
            context_id: ID of the context to switch to
            
        Returns:
            Whether the switch was successful
        """
        switch_start = time.time()
        
        with self._lock:
            if context_id not in self.contexts:
                logger.error(f"Cannot switch to non-existent context {context_id}")
                return False
                
            if self.active_context_id == context_id:
                logger.info(f"Already in context {context_id}")
                return True
                
            # Suspend current context if it exists
            if self.active_context_id and self.active_context_id in self.contexts:
                logger.info(f"Suspending context {self.active_context_id}")
                self.contexts[self.active_context_id].suspend()
            
            # Activate the new context
            self.contexts[context_id].resume()
            self.active_context_id = context_id
            
            # Update metrics
            self.metrics.record_context_switch()
            switch_time_ms = (time.time() - switch_start) * 1000
            self.metrics.timings.add_context_switch_time(switch_time_ms)
            
            logger.info(f"Switched to context {context_id} in {switch_time_ms:.2f}ms")
            return True

    def remove_context(self, context_id: str) -> bool:
        """
        Remove a generation context from memory.
        
        Args:
            context_id: ID of the context to remove
            
        Returns:
            Whether the removal was successful
        """
        with self._lock:
            if context_id not in self.contexts:
                logger.warning(f"Cannot remove non-existent context {context_id}")
                return False
                
            # Can't remove active context
            if context_id == self.active_context_id:
                logger.error(f"Cannot remove active context {context_id}")
                return False
                
            # Remove the context and any associated callbacks
            del self.contexts[context_id]
            if context_id in self.token_callbacks:
                del self.token_callbacks[context_id]
                
            logger.info(f"Removed context {context_id}")
            return True

    def register_token_callback(self, 
                             token_idx: int, 
                             callback: Callable,
                             context_id: Optional[str] = None,
                             extra_data: Optional[Dict[str, Any]] = None) -> None:
        """
        Register a callback to be executed when a specific token is generated.
        
        Args:
            token_idx: Position of the token to trigger on
            callback: Function to call when the token is generated
            context_id: Context to register for, defaults to active context
            extra_data: Additional data to pass to the callback
        """
        with self._lock:
            if context_id is None:
                context_id = self.active_context_id or "default"
                
            if context_id not in self.contexts:
                logger.warning(f"Cannot register callback for non-existent context {context_id}")
                return
                
            callback_state = TokenCallbackState(
                token_idx=token_idx,
                callback=callback,
                context_id=context_id,
                extra_data=extra_data or {}
            )
            
            self.token_callbacks[context_id].append(callback_state)
            logger.debug(f"Registered token callback for idx {token_idx} in context {context_id}")

    def trigger_callbacks(self, token_idx: int, context_id: Optional[str] = None) -> None:
        """
        Trigger callbacks for a specific token position.
        
        Args:
            token_idx: Position of the generated token
            context_id: Context ID, defaults to active context
        """
        if context_id is None:
            context_id = self.active_context_id or "default"
            
        with self._lock:
            if context_id not in self.token_callbacks:
                return
                
            # Find all callbacks for this token position
            to_trigger = []
            remaining = []
            
            for cb_state in self.token_callbacks[context_id]:
                if cb_state.token_idx == token_idx:
                    to_trigger.append(cb_state)
                else:
                    remaining.append(cb_state)
                    
            # Update the callback list
            self.token_callbacks[context_id] = remaining
            
        # Execute the callbacks outside the lock
        for cb_state in to_trigger:
            try:
                cb_state.callback(token_idx, cb_state.extra_data)
            except Exception as e:
                logger.error(f"Error in token callback: {e}", exc_info=True)

    def handle_token_generated(self, token_idx: int, context_id: Optional[str] = None) -> None:
        """
        Notify that a token has been generated.
        
        Args:
            token_idx: Position of the generated token
            context_id: Context ID, defaults to active context
        """
        if context_id is None:
            context_id = self.active_context_id or "default"
            
        # Trigger registered callbacks
        self.trigger_callbacks(token_idx, context_id)
        
        # Update the context's sequence ID
        with self._lock:
            if context_id in self.contexts:
                self.contexts[context_id].sequence_id = max(
                    self.contexts[context_id].sequence_id,
                    token_idx + 1
                )

    def clear_interrupts(self, 
                       context_id: Optional[str] = None,
                       interrupt_type: Optional[InterruptType] = None,
                       priority: Optional[InterruptPriority] = None) -> int:
        """
        Clear interrupts matching the specified filters.
        
        Args:
            context_id: Only clear interrupts for this context
            interrupt_type: Only clear interrupts of this type
            priority: Only clear interrupts with this priority
            
        Returns:
            Number of interrupts cleared
        """
        with self._lock:
            original_count = len(self.interrupt_queue)
            
            # Apply filters
            if context_id or interrupt_type or priority:
                filtered_queue = []
                
                for interrupt in self.interrupt_queue:
                    should_keep = True
                    
                    if context_id and interrupt.context_id != context_id:
                        should_keep = False
                        
                    if interrupt_type and interrupt.type != interrupt_type:
                        should_keep = False
                        
                    if priority and interrupt.priority != priority:
                        should_keep = False
                        
                    if should_keep:
                        filtered_queue.append(interrupt)
                        
                cleared_count = original_count - len(filtered_queue)
                self.interrupt_queue = filtered_queue
            else:
                # Clear all
                cleared_count = original_count
                self.interrupt_queue = []
                
            logger.info(f"Cleared {cleared_count} interrupts")
            self.metrics.interrupts_dropped += cleared_count
            
            return cleared_count

    def get_metrics(self) -> Dict[str, Any]:
        """Get performance metrics about the interrupt handler."""
        with self._lock:
            metrics_copy = self.metrics.get_summary()
            metrics_copy["queue_length"] = len(self.interrupt_queue)
            metrics_copy["active_context"] = self.active_context_id
            metrics_copy["num_contexts"] = len(self.contexts)
            
            # Add priority queue breakdown
            priority_counts = defaultdict(int)
            for interrupt in self.interrupt_queue:
                priority_counts[interrupt.priority.name] += 1
            metrics_copy["queue_by_priority"] = dict(priority_counts)
            
            return metrics_copy

    def update_kv_cache(self, 
                      kv_cache: Dict[str, torch.Tensor], 
                      context_id: Optional[str] = None) -> None:
        """
        Update the KV cache for a context.
        
        Args:
            kv_cache: New KV cache tensors
            context_id: Context to update, defaults to active context
        """
        if context_id is None:
            context_id = self.active_context_id
            
        if not context_id:
            logger.error("Cannot update KV cache: no active context")
            return
            
        with self._lock:
            if context_id not in self.contexts:
                logger.error(f"Cannot update KV cache for non-existent context {context_id}")
                return
                
            # Ensure we don't exceed maximum KV cache size
            for k, v in kv_cache.items():
                if v.size(1) > self.max_kv_cache_size:
                    # Truncate to keep only the most recent tokens
                    kv_cache[k] = v[:, -self.max_kv_cache_size:]
                    logger.warning(
                        f"Truncated KV cache for {context_id} to {self.max_kv_cache_size} tokens"
                    )
                    
            # Update the context
            self.contexts[context_id].kv_cache = kv_cache

    def get_kv_cache(self, context_id: Optional[str] = None) -> Optional[Dict[str, torch.Tensor]]:
        """
        Get the KV cache for a context.
        
        Args:
            context_id: Context to retrieve from, defaults to active context
        """
        if context_id is None:
            context_id = self.active_context_id
            
        if not context_id:
            logger.error("Cannot get KV cache: no active context")
            return None
            
        with self._lock:
            if context_id not in self.contexts:
                logger.error(f"Cannot get KV cache for non-existent context {context_id}")
                return None
                
            return self.contexts[context_id].kv_cache

    def _queue_interrupt(self, interrupt: InterruptRequest) -> None:
        """Add an interrupt to the priority queue."""
        with self._lock:
            # Update sequence ID to be higher than the context's current sequence
            if interrupt.context_id in self.contexts:
                interrupt.sequence_id = self.contexts[interrupt.context_id].sequence_id
            
            # Add to priority queue
            heapq.heappush(self.interrupt_queue, interrupt)
            
            # Track time of last interrupt for latency measurement
            self.last_interrupt_time = time.time()

    def _process_single_interrupt(self, interrupt: InterruptRequest) -> bool:
        """Process a single interrupt."""
        # Track latency
        latency_ms = (time.time() - interrupt.timestamp) * 1000
        self.metrics.timings.add_latency(latency_ms)
        
        # Update state
        old_state = self.current_state
        self.current_state = ModelState.INTERRUPTED
        
        # Process the interrupt
        processing_start = time.time()
        success = False
        
        try:
            # Check for custom handlers first
            custom_handlers = self.custom_handlers.get(interrupt.type, [])
            if custom_handlers:
                for handler in custom_handlers:
                    handler(interrupt)
                success = True
            else:
                # Use default handler
                default_handler = self.default_handlers.get(interrupt.type)
                if default_handler:
                    success = default_handler(interrupt)
                else:
                    logger.warning(f"No handler found for interrupt type {interrupt.type}")
            
            # Mark as handled
            interrupt.handled = success
            
            # Invoke callback if provided
            if interrupt.callback:
                try:
                    interrupt.callback(interrupt)
                except Exception as e:
                    logger.error(f"Error in interrupt callback: {e}", exc_info=True)
                    
            # Update metrics
            if success:
                self.metrics.record_handled()
            else:
                self.metrics.record_dropped()
                
            # Measure handling time
            handling_time = (time.time() - processing_start) * 1000
            self.metrics.timings.add_handling_time(handling_time)
            
            return success
            
        except Exception as e:
            logger.error(f"Error handling interrupt {interrupt.type}: {e}", exc_info=True)
            self.metrics.record_dropped()
            return False
            
        finally:
            # Restore previous state
            self.current_state = old_state

    def _handler_loop(self) -> None:
        """Background thread for processing interrupts."""
        while self.is_running:
            try:
                interrupts_to_process = []
                
                # Get interrupts from queue under lock
                with self._lock:
                    # Copy highest priority interrupts
                    while self.interrupt_queue and len(interrupts_to_process) < 10:
                        interrupt = heapq.heappop(self.interrupt_queue)
                        interrupts_to_process.append(interrupt)
                
                # Process copied interrupts outside lock
                for interrupt in interrupts_to_process:
                    self._process_single_interrupt(interrupt)
                
            except Exception as e:
                logger.error(f"Error in interrupt handler loop: {e}", exc_info=True)
                
            # Sleep for the specified interval
            time.sleep(self.handle_interval_ms / 1000)

    def _heartbeat_loop(self) -> None:
        """Background thread for regular heartbeat interrupts."""
        while self.is_running:
            try:
                # Add a heartbeat interrupt
                self.add_interrupt(
                    InterruptType.HEARTBEAT,
                    priority=InterruptPriority.BACKGROUND,
                    data={"timestamp": time.time()}
                )
            except Exception as e:
                logger.error(f"Error in heartbeat loop: {e}", exc_info=True)
                
            # Sleep for the specified interval
            time.sleep(self.heartbeat_interval_ms / 1000)

    # Default interrupt handlers

    def _handle_new_input(self, interrupt: InterruptRequest) -> bool:
        """Handle new input interrupt."""
        logger.info(f"New input from {interrupt.source_modality}: {interrupt.data}")
        return True

    def _handle_input_end(self, interrupt: InterruptRequest) -> bool:
        """Handle input end interrupt."""
        logger.info(f"Input from {interrupt.source_modality} has ended")
        return True

    def _handle_output_request(self, interrupt: InterruptRequest) -> bool:
        """Handle output request interrupt."""
        logger.info(f"Output requested for {interrupt.target_modality}")
        return True

    def _handle_abort(self, interrupt: InterruptRequest) -> bool:
        """Handle abort interrupt."""
        logger.info(f"Abort requested: {interrupt.data}")
        self.metrics.record_abort()
        return True

    def _handle_context_switch(self, interrupt: InterruptRequest) -> bool:
        """Handle context switch interrupt."""
        target_context = interrupt.data.get("target_context")
        if not target_context:
            logger.error("Context switch interrupt missing target_context")
            return False
            
        return self.switch_context(target_context)

    def _handle_memory_pressure(self, interrupt: InterruptRequest) -> bool:
        """Handle memory pressure interrupt."""
        logger.info("Memory pressure detected, performing cleanup")
        
        # Remove oldest inactive contexts
        with self._lock:
            inactive_contexts = [
                ctx_id for ctx_id, ctx in self.contexts.items() 
                if ctx_id != self.active_context_id and ctx.is_suspended
            ]
            
            if inactive_contexts:
                # Keep only the 2 most recently used contexts
                inactive_contexts.sort(
                    key=lambda ctx_id: self.contexts[ctx_id].timestamp
                )
                
                for ctx_id in inactive_contexts[:-2]:  # Remove all but the 2 newest
                    del self.contexts[ctx_id]
                    logger.info(f"Removed inactive context {ctx_id} due to memory pressure")
                    
                return True
                
        return False

    def _handle_safety_alert(self, interrupt: InterruptRequest) -> bool:
        """Handle safety alert interrupt."""
        logger.warning(f"Safety alert: {interrupt.data}")
        return True

    def _handle_external(self, interrupt: InterruptRequest) -> bool:
        """Handle external system interrupt."""
        logger.info(f"External interrupt: {interrupt.data}")
        return True

    def _handle_heartbeat(self, interrupt: InterruptRequest) -> bool:
        """Handle heartbeat interrupt."""
        logger.debug("Heartbeat interrupt")
        return True

    def _handle_modality_sync(self, interrupt: InterruptRequest) -> bool:
        """Handle modality synchronization interrupt."""
        logger.info(f"Modality sync point: {interrupt.data}")
        return True

# Example usage:
# handler = InterruptHandler()
# handler.start()
# handler.add_interrupt(InterruptType.NEW_INPUT, data={'type': 'text', 'content': 'Hello'})
# handler.process_interrupts()
# handler.stop()

