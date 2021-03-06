import copy
import os
import shlex
import subprocess
import sys
import time
from timeit import default_timer as timer

from mpi4py import MPI

import numpy as np


class runner(object):
    def __init__(
        self,
        base_input_dir,
        Solver,
        nprocs_per_solver,
        comm,
        perturb=0,
        nthreads_per_proc=1,
        solver_run_scheme="mpi_spawn_ready",
    ):
        self.solver_name = Solver.name()
        self.path_to_solver = Solver.path_to_solver
        self.base_solver_input = Solver.input
        self.base_solver_input.from_directory(base_input_dir)
        self.nprocs_per_solver = nprocs_per_solver
        self.nthreads_per_proc = nthreads_per_proc
        self.output = Solver.output
        self.comm = comm
        if solver_run_scheme not in Solver.solver_run_schemes():
            print(
                "{scheme} not implemented for {solver}".format(
                    scheme=solver_run_scheme, solver=Solver.name()
                )
            )
            sys.exit(1)
        if solver_run_scheme == "mpi_spawn_ready":
            self.run = run_mpispawn_ready(
                self.path_to_solver, nprocs_per_solver, nthreads_per_proc, comm
            )
        elif solver_run_scheme == "mpi_spawn":
            self.run = run_mpispawn(
                self.path_to_solver, nprocs_per_solver, nthreads_per_proc, comm
            )
        elif solver_run_scheme == "subprocess":
            self.run = run_subprocess(
                self.path_to_solver, nprocs_per_solver, nthreads_per_proc, comm
            )
        elif solver_run_scheme == "python_module":
            print("{scheme} not implemented yet".format(scheme=solver_run_scheme))
            sys.exit(1)
        self.perturb = perturb

    def submit(self, structure, output_dir, seldyn_arr=None):
        if self.perturb:
            structure.perturb(self.perturb)
        solverinput = self.base_solver_input
        solverinput.update_info_by_structure(structure, seldyn_arr)
        self.run.submit(self.solver_name, solverinput, output_dir)
        results = self.output.get_results(output_dir)
        return np.float64(results.energy), results.structure


class runner_multistep(object):
    def __init__(
        self, base_input_dirs, Solver, runner, nprocs_per_solver, comm, perturb=0
    ):
        self.runners = []
        assert len(base_input_dirs) > 1
        self.runners.append(
            runner(
                base_input_dirs[0],
                copy.deepcopy(Solver),
                nprocs_per_solver,
                comm,
                perturb,
            )
        )
        for i in range(1, len(base_input_dirs)):
            self.runners.append(
                runner(
                    base_input_dirs[i],
                    copy.deepcopy(Solver),
                    nprocs_per_solver,
                    comm,
                    perturb=0,
                )
            )

    def submit(self, structure, output_dir, seldyn_arr=None):
        energy, newstructure = self.runners[0].submit(structure, output_dir, seldyn_arr)
        for i in range(1, len(self.runners)):
            energy, newstructure = self.runners[i].submit(
                newstructure, output_dir, seldyn_arr
            )
        return energy, newstructure


def submit_bulkjob(solverrundirs, path_to_solver, n_mpiprocs, n_ompthreads):
    joblist = open("joblist.txt", "w")
    if n_ompthreads != 1:
        progtype = "H" + str(n_ompthreads)
    else:
        progtype = "M"
    for solverrundir in solverrundirs:
        joblist.write(
            ";".join([path_to_solver, str(n_mpiprocs), progtype, solverrundir]) + "\n"
        )
    stdout = open("stdout.log", "w")
    stderr = open("stderr.log", "w")
    stdin = open(os.devnull, "r")
    joblist.flush()
    start = timer()
    p = subprocess.Popen(
        "bulkjob ./joblist.txt", stdout=stdout, stderr=stderr, stdin=stdin, shell=True
    )
    exitcode = p.wait()
    end = timer()
    print("it took ", end - start, " secs. to start vasp and finish")
    sys.stdout.flush()
    return exitcode


class run_mpibulkjob:
    def __init__(self, path_to_spawn_ready_vasp, nprocs, comm):
        self.path_to_vasp = path_to_spawn_ready_vasp
        self.nprocs = nprocs
        self.comm = comm
        self.commsize = comm.Get_size()
        self.commrank = comm.Get_rank()

    def submit(self, solverinput, output_dir):
        solverinput.write_input(output_dir=output_dir)
        solverrundirs = self.comm.gather(output_dir, root=0)
        exitcode = 1
        if self.commrank == 0:
            exitcode = np.array(
                [submit_bulkjob(solverrundirs, self.path_to_vasp, self.nprocs, 1)]
            )
            for i in range(1, self.commsize):
                self.comm.Isend([exitcode, MPI.INT], dest=i, tag=i)

        else:
            exitcode = np.array([0])
            while not self.comm.Iprobe(source=0, tag=self.commrank):
                time.sleep(0.2)
            self.comm.Recv([exitcode, MPI.INT], source=0, tag=self.commrank)
        return exitcode[0]


class run_mpispawn:
    def __init__(self, path_to_solver, nprocs, nthreads, comm):
        self.path_to_solver = path_to_solver
        self.nprocs = nprocs
        self.nthreads = nthreads
        self.comm = comm
        self.commsize = comm.Get_size()
        self.commrank = comm.Get_rank()
        commworld = MPI.COMM_WORLD
        self.worldrank = commworld.Get_rank()

    def submit(self, solver_name, solverinput, output_dir, rerun=2):
        solverinput.write_input(output_dir=output_dir)

        # Barrier so that spawn is atomic between processes.
        # This is to make sure that vasp processes are spawned one by one according to
        # MPI policy (hopefully on adjacent nodes)
        # (might be MPI implementation dependent...)

        # for i in range(self.commsize):
        #    self.comm.Barrier()
        #    if i == self.commrank:
        failed_dir = []
        cl_argslist = self.comm.gather(
            solverinput.cl_args(self.nprocs, self.nthreads, output_dir), root=0
        )
        solverrundirs = self.comm.gather(output_dir, root=0)

        checkfilename = 'abacus_solver_finished'

        if self.commrank == 0:
            for rundir in solverrundirs:
                solverinput.cleanup(rundir)

            # wrappers = [
            #     "rm -f {checkfile}; {solvername} {cl_args}; echo $? > {checkfile}".format(
            #         checkfile=shlex.quote(
            #             os.path.join(rundir, checkfilename)
            #         ),
            #         solvername=self.path_to_solver,
            #         cl_args=" ".join(map(shlex.quote, cl_args)),
            #     )
            #     for cl_args in cl_argslist
            # ]
            #
            # start = timer()
            # commspawn = [
            #     MPI.COMM_SELF.Spawn(
            #         os.getenv('SHELL'), args=["-c", wrapper], maxprocs=self.nprocs
            #     )
            #     for wrapper in wrappers
            # ]

            start = timer()
            commspawn = [
                MPI.COMM_SELF.Spawn(
                    self.path_to_solver, args=cl_args, maxprocs=self.nprocs
                )
                for cl_args in cl_argslist
            ]
            end = timer()
            print("rank ", self.worldrank, " took ", end - start, " to spawn")
            sys.stdout.flush()
            start = timer()
            for rundir in solverrundirs:
                while True:
                    # if os.path.exists(os.path.join(rundir, checkfilename)):
                    if solverinput.check_finished(rundir):
                        break
                    time.sleep(1)
            end = timer()
            print(
                "rank ",
                self.worldrank,
                " took ",
                end - start,
                " for " + solver_name + "execution",
            )

            if len(failed_dir) != 0:
                print(
                    solver_name + " failed in directories: \n " + "\n".join(failed_dir)
                )
                sys.stdout.flush()
                if rerun == 0:
                    MPI.COMM_WORLD.Abort()
        self.comm.Barrier()

        # Rerun if Solver failed
        failed_dir = self.comm.bcast(failed_dir, root=0)
        if len(failed_dir) != 0:
            solverinput.update_info_from_files(output_dir, rerun)
            rerun -= 1
            self.submit(solverinput, output_dir, rerun)

        return 0


class run_mpispawn_ready:
    def __init__(self, path_to_spawn_ready_solver, nprocs, nthreads, comm):
        self.path_to_solver = path_to_spawn_ready_solver
        self.nprocs = nprocs
        self.nthreads = nthreads
        self.comm = comm
        self.commsize = comm.Get_size()
        self.commrank = comm.Get_rank()
        commworld = MPI.COMM_WORLD
        self.worldrank = commworld.Get_rank()

    def submit(self, solver_name, solverinput, output_dir, rerun=2):
        solverinput.write_input(output_dir=output_dir)

        # Barrier so that spawn is atomic between processes.
        # This is to make sure that vasp processes are spawned one by one according to
        # MPI policy (hopefully on adjacent nodes)
        # (might be MPI implementation dependent...)

        # for i in range(self.commsize):
        #    self.comm.Barrier()
        #    if i == self.commrank:
        failed_dir = []
        cl_argslist = self.comm.gather(
            solverinput.cl_args(self.nprocs, self.nthreads, output_dir), root=0
        )
        solverrundirs = self.comm.gather(output_dir, root=0)
        if self.commrank == 0:
            start = timer()
            commspawn = [
                MPI.COMM_SELF.Spawn(
                    self.path_to_solver,  # ex. /home/issp/vasp/vasp.5.3.5/bin/vasp",
                    args=cl_args,
                    maxprocs=self.nprocs,
                )
                for cl_args in cl_argslist
            ]
            end = timer()
            print("rank ", self.worldrank, " took ", end - start, " to spawn")
            sys.stdout.flush()
            start = timer()
            exitcode = np.array(0, dtype=np.intc)
            i = 0
            for comm in commspawn:
                comm.Bcast([exitcode, MPI.INT], root=0)
                comm.Disconnect()
                if exitcode != 0:
                    failed_dir.append(solverrundirs[i])
                i = i + 1
            end = timer()
            print(
                "rank ",
                self.worldrank,
                " took ",
                end - start,
                " for " + solver_name + "execution",
            )

            if len(failed_dir) != 0:
                print(
                    solver_name + " failed in directories: \n " + "\n".join(failed_dir)
                )
                sys.stdout.flush()
                if rerun == 0:
                    MPI.COMM_WORLD.Abort()
        self.comm.Barrier()

        # Rerun if Solver failed
        failed_dir = self.comm.bcast(failed_dir, root=0)
        if len(failed_dir) != 0:
            solverinput.update_info_from_files(output_dir, rerun)
            rerun -= 1
            self.submit(solver_name, solverinput, output_dir, rerun)

        # commspawn = MPI.COMM_SELF.Spawn(self.path_to_vasp, #/home/issp/vasp/vasp.5.3.5/bin/vasp",
        #                                args=[output_dir],
        #                                   maxprocs=self.nprocs)

        # Spawn is too slow, can't afford to make it atomic
        # commspawn = MPI.COMM_SELF.Spawn(self.path_to_vasp, #/home/issp/vasp/vasp.5.3.5/bin/vasp",
        #                               args=[output_dir,],
        #                               maxprocs=self.nprocs)
        #        sendbuffer = create_string_buffer(output_dir.encode('utf-8'),255)
        #        commspawn.Bcast([sendbuffer, 255, MPI.CHAR], root=MPI.ROOT)
        # commspawn.Barrier()
        # commspawn.Disconnect()
        # os.chdir(cwd)
        return 0


class run_subprocess:
    def __init__(self, path_to_solver, nprocs, nthreads, comm):
        self.path_to_solver = path_to_solver
        self.nprocs = nprocs
        self.nthreads = nthreads

    def submit(self, solver_name, solverinput, output_dir, rerun=0):
        solverinput.write_input(output_dir=output_dir)
        cwd = os.getcwd()
        os.chdir(output_dir)
        args = solverinput.cl_args(self.nprocs, self.nthreads, output_dir)
        command = [self.path_to_solver]
        command.extend(args)
        with open("{}/stdout".format(output_dir), "w") as fi:
            res = subprocess.run(
                command, stdout=fi, stderr=subprocess.STDOUT, check=True
            )
        os.chdir(cwd)
        return 0
