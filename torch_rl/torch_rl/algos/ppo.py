import numpy
import torch
import torch.nn.functional as F

from torch_rl.algos.base import BaseAlgo

class PPOAlgo(BaseAlgo):
    def __init__(self, envs, acmodel, num_frames_per_proc=None, discount=0.99, lr=7e-4, gae_tau=0.95,
                 entropy_coef=0.01, value_loss_coef=0.5, max_grad_norm=0.5, recurrence=4,
                 adam_eps=1e-5, clip_eps=0.2, epochs=4, batch_size=256, preprocess_obss=None,
                 reshape_reward=None):
        num_frames_per_proc = num_frames_per_proc or 128

        super().__init__(envs, acmodel, num_frames_per_proc, discount, lr, gae_tau, entropy_coef,
                         value_loss_coef, max_grad_norm, recurrence, preprocess_obss, reshape_reward)

        self.clip_eps = clip_eps
        self.epochs = epochs
        self.batch_size = batch_size

        assert self.batch_size % self.recurrence == 0

        self.optimizer = torch.optim.Adam(self.acmodel.parameters(), lr, eps=adam_eps)

        self.batch_num = 0
    
    def update_parameters(self):
        # Collect experiences

        exps, logs = self.collect_experiences()

        for _ in range(self.epochs):
            # Initialize log values

            log_entropies = []
            log_values = []
            log_policy_losses = []
            log_value_losses = []

            for inds in self.batches_starting_indexes():
                # Initialize batch values

                batch_entropy = 0
                batch_value = 0
                batch_policy_loss = 0
                batch_value_loss = 0
                batch_loss = 0

                # Initialize memory

                if self.acmodel.recurrent:
                    memory = exps.memory[inds]

                for i in range(self.recurrence):
                    # Create a sub-batch of experience

                    sb = exps[inds]

                    # Compute loss

                    if self.acmodel.recurrent:
                        dist, value, memory = self.acmodel(sb.obs, memory * sb.mask)
                    else:
                        dist, value = self.acmodel(sb.obs)

                    entropy = dist.entropy().mean()

                    ratio = torch.exp(dist.log_prob(sb.action) - sb.log_prob)
                    surr1 = ratio * sb.advantage
                    surr2 = torch.clamp(ratio, 1.0 - self.clip_eps, 1.0 + self.clip_eps) * sb.advantage
                    policy_loss = -torch.min(surr1, surr2).mean()

                    value_clipped = sb.value + torch.clamp(value - sb.value, -self.clip_eps, self.clip_eps)
                    surr1 = (value - sb.returnn).pow(2)
                    surr2 = (value_clipped - sb.returnn).pow(2)
                    value_loss = torch.max(surr1, surr2).mean()

                    loss = policy_loss - self.entropy_coef * entropy + self.value_loss_coef * value_loss

                    # Update batch values

                    batch_entropy += entropy.item()
                    batch_value += value.mean().item()
                    batch_policy_loss += policy_loss.item()
                    batch_value_loss += value_loss.item()
                    batch_loss += loss

                    # Update indexes

                    inds += 1

                    # Update memories for next epoch

                    if self.acmodel.recurrent and i < self.recurrence - 1:
                        exps.memory[inds] = memory.detach()

                # Update batch values

                batch_entropy /= self.recurrence
                batch_value /= self.recurrence
                batch_policy_loss /= self.recurrence
                batch_value_loss /= self.recurrence
                batch_loss /= self.recurrence

                # Update actor-critic

                self.optimizer.zero_grad()
                batch_loss.backward()
                torch.nn.utils.clip_grad_norm_(self.acmodel.parameters(), self.max_grad_norm)
                self.optimizer.step()

                # Update log values

                log_entropies.append(batch_entropy)
                log_values.append(batch_value)
                log_policy_losses.append(batch_policy_loss)
                log_value_losses.append(batch_value_loss)
        
        # Log some values

        logs["entropy"] = numpy.mean(log_entropies)
        logs["value"] = numpy.mean(log_values)
        logs["policy_loss"] = numpy.mean(log_policy_losses)
        logs["value_loss"] = numpy.mean(log_value_losses)

        return logs
    
    def batches_starting_indexes(self):
        indexes = numpy.arange(0, self.num_frames, self.recurrence)
        indexes = numpy.random.permutation(indexes)

        # Shift starting indexes by self.recurrence//2 half the time
        if self.batch_num % 2 == 1:
            indexes = indexes[(indexes + self.recurrence) % self.batch_size != 0]
            indexes += self.recurrence // 2
        self.batch_num += 1
        
        num_indexes = self.batch_size // self.recurrence
        batches_indexes = [indexes[i:i+num_indexes] for i in range(0, len(indexes), num_indexes)]
        
        return batches_indexes